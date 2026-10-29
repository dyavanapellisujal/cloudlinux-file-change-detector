"""
FanotifyWatcher — the PRIMARY change source on Linux.

Why fanotify (and not inotify): one `FAN_MARK_FILESYSTEM` mark watches the whole
filesystem under SEED_TREE — every account, every sub-directory, and any
directory created later — with a single call. inotify needs one watch per
directory, doesn't recurse, hits `max_user_watches`, and misses changes during
the long initial walk. For 2000 accounts and millions of files, fanotify is the
one that scales.

Requirements (documented in RATIONALE):
  * root / CAP_SYS_ADMIN      — to mark a whole filesystem
  * Linux 5.9+                — for FAN_REPORT_DFID_NAME, which reports the
                                parent directory's file handle + the filename so
                                we can rebuild the absolute path.

Events we subscribe to, and how they map to a scan reason:
    FAN_CREATE       a file appeared            -> "created"
    FAN_MOVED_TO     a file was moved into place-> "replaced"  (atomic saves)
    FAN_CLOSE_WRITE  a file was written+closed  -> "modified"  (fires ONCE per
                     close, not per write() — kills most duplicate noise for free)
    FAN_ATTRIB       permissions changed        -> re-stat and apply the +x rule

The reader stays deliberately dumb and fast (FAN_UNLIMITED_QUEUE + minimal work
per event) so a 50k-file burst can't overflow the kernel queue and make us miss
changes. All heavy lifting (filtering, dedup) happens downstream in the queue.

If fanotify can't be set up (not root, old kernel), __init__ raises and the
detector falls back to PollWatcher.
"""
from __future__ import annotations

import ctypes
import os
import stat
import struct
import select
import sys

# --------------------------------------------------------------------------- #
#  libc + syscall signatures
# --------------------------------------------------------------------------- #
_libc = ctypes.CDLL(None, use_errno=True)
_libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
_libc.fanotify_mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64,
                                ctypes.c_int, ctypes.c_char_p]
_libc.open_by_handle_at.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]

# --------------------------------------------------------------------------- #
#  fanotify constants (from <linux/fanotify.h>)
# --------------------------------------------------------------------------- #
FAN_CLOEXEC = 0x00000001
FAN_NONBLOCK = 0x00000002
FAN_CLASS_NOTIF = 0x00000000
FAN_UNLIMITED_QUEUE = 0x00000010
FAN_REPORT_DIR_FID = 0x00000400
FAN_REPORT_NAME = 0x00000800
FAN_REPORT_DFID_NAME = FAN_REPORT_DIR_FID | FAN_REPORT_NAME

FAN_MARK_ADD = 0x00000001
FAN_MARK_FILESYSTEM = 0x00000100

FAN_MODIFY = 0x00000002
FAN_ATTRIB = 0x00000004
FAN_CLOSE_WRITE = 0x00000008
FAN_MOVED_TO = 0x00000080
FAN_CREATE = 0x00000100
FAN_Q_OVERFLOW = 0x00004000   # kernel event queue overflowed -> we may have missed changes

WATCH_MASK = FAN_CREATE | FAN_MOVED_TO | FAN_CLOSE_WRITE | FAN_ATTRIB

# info record types that carry a file handle
FAN_INFO_FID = 1          # the object's own handle (no name)
FAN_INFO_DFID_NAME = 2    # parent dir handle + filename
FAN_INFO_DFID = 3         # parent dir handle (no name)

AT_FDCWD = -100
O_PATH = 0x00200000

# struct fanotify_event_metadata: event_len, vers, reserved, metadata_len,
#                                 mask (u64), fd (s32), pid (s32)
_META = struct.Struct("<IBBHQii")
_INFO_HDR = struct.Struct("<BBH")       # info_type, pad, len


def _log(msg: str) -> None:
    print(f"[detector/fanotify] {msg}", file=sys.stderr, flush=True)


def _reason_for(mask: int) -> str:
    if mask & FAN_CREATE:
        return "created"
    if mask & FAN_MOVED_TO:
        return "replaced"
    return "modified"   # CLOSE_WRITE / MODIFY / ATTRIB


class FanotifyWatcher:
    def __init__(self, roots, callback, stop_event):
        # `roots` is one path or a list of paths — one per filesystem to watch.
        self.roots = [roots] if isinstance(roots, str) else list(roots)
        self.callback = callback        # callback(path, reason, size, mtime_ns, is_exec)
        self.stop = stop_event

        flags = (FAN_CLASS_NOTIF | FAN_CLOEXEC | FAN_NONBLOCK
                 | FAN_UNLIMITED_QUEUE | FAN_REPORT_DFID_NAME)
        self.fan_fd = _libc.fanotify_init(flags, os.O_RDONLY)
        if self.fan_fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"fanotify_init failed: {os.strerror(err)}")

        # A fanotify FILESYSTEM mark covers ONE superblock, so each separately
        # mounted disk needs its own mark — but all on this single fanotify
        # handle, so every disk's events share one queue and one pipeline (one
        # service, not one-per-disk). We also keep one fd per watched root as a
        # mount reference for open_by_handle_at(); _resolve() tries them until
        # the handle resolves, which is how path resolution works across disks.
        self.mount_fds = []
        for root in self.roots:
            rc = _libc.fanotify_mark(self.fan_fd, FAN_MARK_ADD | FAN_MARK_FILESYSTEM,
                                     WATCH_MASK, AT_FDCWD, root.encode())
            if rc < 0:
                err = ctypes.get_errno()
                _log(f"WARNING: cannot mark {root}: {os.strerror(err)} (skipped)")
                continue
            self.mount_fds.append(os.open(root, os.O_RDONLY))
        if not self.mount_fds:
            os.close(self.fan_fd)
            raise OSError("fanotify_mark failed for every watched root")
        _log(f"watching {len(self.mount_fds)} filesystem(s): {', '.join(self.roots)}")

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        try:
            while not self.stop.is_set():
                r, _, _ = select.select([self.fan_fd], [], [], 0.5)
                if not r:
                    continue
                data = os.read(self.fan_fd, 1 << 20)   # read many events at once
                self._handle_buffer(data)
        finally:
            for fd in [self.fan_fd, *self.mount_fds]:
                try:
                    os.close(fd)
                except OSError:
                    pass

    # ------------------------------------------------------------------ #
    def _handle_buffer(self, data: bytes) -> None:
        off = 0
        n = len(data)
        while off + _META.size <= n:
            event_len, _vers, _res, meta_len, mask, _fd, _pid = _META.unpack_from(data, off)
            if event_len < _META.size:
                break
            if mask & FAN_Q_OVERFLOW:
                # We asked for FAN_UNLIMITED_QUEUE so this should never happen, but
                # if it does we must NOT fail silently: shout so a rescan can recover.
                _log("WARNING: FAN_Q_OVERFLOW — kernel queue overflowed, changes may "
                     "have been missed; a rescan of the tree is required to stay complete")
                off += event_len
                continue
            dir_path, name = self._parse_info(data, off + meta_len, off + event_len)
            if dir_path is not None:
                full = dir_path if name is None else os.path.join(dir_path, name)
                self._emit(full, mask)
            off += event_len

    def _parse_info(self, data: bytes, start: int, end: int):
        """Walk the info records after the metadata; return (dir_path, name)."""
        dir_path = None
        name = None
        p = start
        while p + _INFO_HDR.size <= end:
            info_type, _pad, info_len = _INFO_HDR.unpack_from(data, p)
            if info_len <= 0:
                break
            if info_type in (FAN_INFO_FID, FAN_INFO_DFID, FAN_INFO_DFID_NAME):
                # layout: header(4) | fsid(8) | file_handle(..) | [name\0]
                fh_off = p + _INFO_HDR.size + 8
                handle_bytes = struct.unpack_from("<I", data, fh_off)[0]
                fh_total = 8 + handle_bytes          # handle_bytes(4) + type(4) + f_handle
                fh = data[fh_off:fh_off + fh_total]
                resolved = self._resolve(fh)
                if resolved is not None:
                    dir_path = resolved
                if info_type == FAN_INFO_DFID_NAME:
                    raw = data[fh_off + fh_total:p + info_len]
                    raw = raw.split(b"\x00", 1)[0]
                    if raw and raw != b".":
                        name = raw.decode("utf-8", "surrogateescape")
            p += info_len
        return dir_path, name

    def _resolve(self, fh_bytes: bytes):
        """Turn a file handle into a path via open_by_handle_at + /proc/self/fd.

        A handle only resolves against a mount fd on its OWN filesystem, so with
        several watched disks we try each until one works (EINVAL/ESTALE on the
        others). For a handful of mounts this is cheap and avoids fsid bookkeeping.
        """
        buf = ctypes.create_string_buffer(fh_bytes, len(fh_bytes))
        for mount_fd in self.mount_fds:
            fd = _libc.open_by_handle_at(mount_fd, buf, O_PATH)
            if fd < 0:
                continue                      # not this fs (or object gone) — try next
            try:
                return os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                return None
            finally:
                os.close(fd)
        return None

    def _emit(self, path: str, mask: int) -> None:
        try:
            st = os.stat(path)
        except OSError:
            return                            # created-then-deleted, etc.
        if not stat.S_ISREG(st.st_mode):
            return                            # we only scan regular files
        is_exec = bool(st.st_mode & 0o111)
        self.callback(path, _reason_for(mask), st.st_size, st.st_mtime_ns, is_exec)
