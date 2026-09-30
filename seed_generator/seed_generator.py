#!/usr/bin/env python3
"""
Seed-dataset generator — lays down a realistic cPanel/WordPress-shaped shared-hosting tree so
the candidate's detector has something to point at and the change-load generator has real files
to mutate. Deterministic (same --seed => identical tree), scale-configurable, mechanism-neutral.

See seed-generator-spec.md. Emits a manifest with curated pools (watch_targets / noise_targets /
plusx_pool) that the change-load generator draws from.

  python3 seed_generator.py --dry-run                    # planned counts + bytes, writes nothing
  python3 seed_generator.py --root /tmp/seed/home --accounts 5
  python3 seed_generator.py --clean --manifest seed-manifest.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor

# size ranges per kind, in KB (lo, hi)
SIZE_KB = {
    "php": (1, 20), "js": (1, 50), "css": (1, 30), "html": (1, 15), "htaccess": (0.2, 1),
    "image": (10, 250), "log": (5, 100), "tmp": (1, 40), "swp": (1, 12), "session": (1, 4),
    "text": (1, 8),
}
RELEVANT = {"php", "js", "html", "htaccess"}            # watch-set relevant kinds
NOISE = {"log", "tmp", "swp", "session", "cache"}        # excluded kinds
_FILLER = b"x" * 65536
_MAGIC = {"image": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"}   # JPEG-ish header so type sniffing looks real
MANIFEST_SCHEMA_VERSION = 1                                # bump when the manifest shape changes


class Entry:
    __slots__ = ("path", "kind", "size", "execu")

    def __init__(self, path: str, kind: str, size: int, execu: bool = False):
        self.path, self.kind, self.size, self.execu = path, kind, size, execu


def _size(kind: str, rng: random.Random, max_kb: float) -> int:
    lo, hi = SIZE_KB.get(kind, (1, 8))
    kb = min(rng.uniform(lo, hi), max_kb)
    return max(64, int(kb * 1024))


def _account_id(idx: int) -> str:
    return f"u{1001 + idx}"


def _base_dir(account: str, idx: int, layout: str, rng: random.Random) -> str:
    da = layout == "directadmin" or (layout == "mixed" and idx % 2 == 1)
    if da:
        return f"{account}/domains/{account}.example.com/public_html"
    return f"{account}/public_html"


def plan_account(idx: int, seed: int, files_target: int, addon_fraction: float,
                 layout: str, max_kb: float) -> list[Entry]:
    """Deterministically plan every file for one account (no I/O). Used by generate AND dry-run."""
    rng = random.Random(f"{seed}:{idx}")
    account = _account_id(idx)
    base = _base_dir(account, idx, layout, rng)
    entries: list[Entry] = []

    roots = [base]
    if rng.random() < addon_fraction:
        roots.append(f"{base}/shop")

    per_root = max(50, files_target // len(roots))
    for root in roots:
        _plan_docroot(entries, rng, root, per_root, max_kb)
    return entries


def _plan_docroot(entries: list[Entry], rng: random.Random, base: str, n: int, max_kb: float) -> None:
    def add(path: str, kind: str, execu: bool = False) -> None:
        entries.append(Entry(f"{base}/{path}", kind, _size(kind, rng, max_kb), execu))

    # top-level
    for f, k in [("index.php", "php"), ("wp-config.php", "php"), ("xmlrpc.php", "php"),
                 ("wp-login.php", "php"), (".htaccess", "htaccess"), ("readme.html", "html"),
                 ("wp-settings.php", "php")]:
        add(f, k)

    remaining = max(0, n - 7)
    n_img = int(remaining * 0.42)
    n_core = int(remaining * 0.18)
    n_plug = int(remaining * 0.20)
    n_theme = int(remaining * 0.06)
    n_js = int(remaining * 0.07)
    n_css = int(remaining * 0.03)
    n_html = int(remaining * 0.02)
    n_noise = max(0, remaining - (n_img + n_core + n_plug + n_theme + n_js + n_css + n_html))

    # uploads/<year>/<month>/ images (the bulk)
    i = 0
    while i < n_img:
        y = rng.choice((2023, 2024, 2025)); m = rng.randint(1, 12)
        for _ in range(min(rng.randint(8, 16), n_img - i)):
            add(f"wp-content/uploads/{y}/{m:02d}/img_{i:05d}.jpg", "image"); i += 1

    # wp-includes core php across subdirs
    sub = ["", "blocks/", "rest-api/", "widgets/", "sodium_compat/", "js/dist/", "css/"]
    for j in range(n_core):
        add(f"wp-includes/{rng.choice(sub)}core_{j:04d}.php", "php")

    # plugins/<plugin>/… with includes/admin/assets subdirs
    nplug = max(6, n_plug // 10)
    for p in range(nplug):
        pdir = f"wp-content/plugins/plugin-{p:02d}"
        add(f"{pdir}/plugin-{p:02d}.php", "php")
        add(f"{pdir}/readme.txt", "text")
        add(f"{pdir}/.htaccess", "htaccess")
    for j in range(n_plug):
        p = rng.randint(0, nplug - 1)
        sd = rng.choice(["includes", "admin", "src", "lib"])
        execu = rng.random() < 0.04        # a few already-executable scripts
        add(f"wp-content/plugins/plugin-{p:02d}/{sd}/mod_{j:04d}.php", "php", execu)

    # themes
    ntheme = max(3, n_theme // 8)
    for t in range(ntheme):
        tdir = f"wp-content/themes/theme-{t:02d}"
        add(f"{tdir}/style.css", "css")
        add(f"{tdir}/functions.php", "php")
        add(f"{tdir}/index.php", "php")
    for j in range(n_theme):
        t = rng.randint(0, ntheme - 1)
        add(f"wp-content/themes/theme-{t:02d}/parts/tpl_{j:04d}.php", "php")

    # js / css assets
    for j in range(n_js):
        p = rng.randint(0, nplug - 1)
        add(f"wp-content/plugins/plugin-{p:02d}/assets/js/app_{j:04d}.js", "js")
    for j in range(n_css):
        p = rng.randint(0, nplug - 1)
        add(f"wp-content/plugins/plugin-{p:02d}/assets/css/style_{j:04d}.css", "css")

    # html
    for j in range(n_html):
        add(f"wp-content/uploads/landing_{j:04d}.html", "html")

    # noise — must NOT become jobs
    for j in range(n_noise):
        r = rng.random()
        if r < 0.4:
            add(f"wp-content/cache/page/c_{j:05d}.tmp", "tmp")
        elif r < 0.6:
            add(f"wp-content/sessions/sess_{j:05d}", "session")
        elif r < 0.8:
            add(f"logs/error_{j:04d}.log", "log")
        elif r < 0.92:
            add(f"wp-content/plugins/plugin-00/src/.mod_{j:04d}.php.swp", "swp")
        else:
            add(f"wp-content/uploads/draft_{j:04d}.html~", "swp")


# --------------------------------------------------------------------------- #
def _write(path: str, entry: Entry, fsync: bool) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    header = _MAGIC.get(entry.kind, b"")
    with open(path, "wb") as fh:
        fh.write(header)
        rem = entry.size - len(header)
        while rem > 0:
            chunk = _FILLER if rem >= len(_FILLER) else _FILLER[:rem]
            fh.write(chunk); rem -= len(chunk)
        if fsync:
            fh.flush(); os.fsync(fh.fileno())
    if entry.execu:
        os.chmod(path, 0o755)


def _pools(entries: list[Entry], root: str) -> dict:
    relevant = [e for e in entries if e.kind in RELEVANT]
    noise = [e for e in entries if e.kind in NOISE]
    plusx = [e for e in entries if e.kind == "php" and not e.execu]
    ap = lambda e: os.path.join(root, e.path)
    return {
        "watch_targets": [ap(e) for e in relevant[:40]],
        "noise_targets": [ap(e) for e in noise[:40]],
        "plusx_pool": [ap(e) for e in plusx[:25]],
    }


def _generate_account(task: tuple) -> dict:
    idx, seed, files_target, addon, layout, max_kb, root, fsync, chown = task
    entries = plan_account(idx, seed, files_target, addon, layout, max_kb)
    dirs: set[str] = set()
    total_bytes = 0
    for e in entries:
        full = os.path.join(root, e.path)
        _write(full, e, fsync)
        dirs.add(os.path.dirname(e.path))
        total_bytes += e.size
    account = _account_id(idx)
    if chown and os.geteuid() == 0:
        uid = 5000 + idx
        for dp, _, fs in os.walk(os.path.join(root, account)):
            os.chown(dp, uid, uid)
            for f in fs:
                os.chown(os.path.join(dp, f), uid, uid)
    doc_roots = sorted({os.path.join(root, e.path.split("/public_html")[0] + "/public_html")
                        for e in entries if "/public_html" in e.path})
    return {
        "id": account,
        "doc_roots": doc_roots,
        "counts": {"files": len(entries), "dirs": len({d for d in dirs}), "bytes": total_bytes},
        **_pools(entries, root),
    }


def _plan_only(task: tuple) -> dict:
    idx, seed, files_target, addon, layout, max_kb = task[:6]
    entries = plan_account(idx, seed, files_target, addon, layout, max_kb)
    dirs = {os.path.dirname(e.path) for e in entries}
    return {"files": len(entries), "dirs": len(dirs),
            "bytes": sum(e.size for e in entries)}


# --------------------------------------------------------------------------- #
def _is_heavy(idx: int, seed: int, frac: float) -> bool:
    return random.Random(f"{seed}:heavy:{idx}").random() < frac


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Seed-dataset generator (challenge fixture).")
    p.add_argument("--root", default="/tmp/seed/home")
    p.add_argument("--accounts", type=int, default=10)   # laptop-safe; RUNBOOK pairs with --files-per-account 800 (~250 MB). Reviewer harness always passes --accounts explicitly.
    p.add_argument("--files-per-account", type=int, default=3000)
    p.add_argument("--heavy-fraction", type=float, default=0.05)
    p.add_argument("--heavy-factor", type=int, default=10)
    p.add_argument("--addon-fraction", type=float, default=0.2)
    p.add_argument("--layout", choices=["cpanel", "directadmin", "mixed"], default="cpanel")
    p.add_argument("--max-file-kb", type=float, default=256)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    p.add_argument("--manifest", default="seed-manifest.json")
    p.add_argument("--chown", action="store_true", help="own each account by a distinct uid (root only)")
    p.add_argument("--fsync", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="print planned counts/bytes, write nothing")
    p.add_argument("--clean", action="store_true", help="remove the tree recorded in --manifest")
    p.add_argument("--force", action="store_true", help="allow writing into a non-empty root")
    args = p.parse_args(argv)

    root = os.path.abspath(args.root)
    if args.clean:
        return _clean(args.manifest, root)

    if root in ("/", os.path.expanduser("~")):
        print(f"refusing to use unsafe root: {root}", file=sys.stderr); return 2

    def files_for(idx: int) -> int:
        f = args.files_per_account
        return f * args.heavy_factor if _is_heavy(idx, args.seed, args.heavy_fraction) else f

    # planning tasks (addon decided deterministically inside plan_account via rng)
    base_tasks = [(i, args.seed, files_for(i), args.addon_fraction, args.layout, args.max_file_kb)
                  for i in range(args.accounts)]

    if args.dry_run:
        tot = {"files": 0, "dirs": 0, "bytes": 0}
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            for r in ex.map(_plan_only, base_tasks, chunksize=4):
                for k in tot:
                    tot[k] += r[k]
        print("DRY RUN — nothing written")
        print(f"  accounts:    {args.accounts} (layout={args.layout}, heavy~{args.heavy_fraction:.0%} ×{args.heavy_factor})")
        print(f"  files:       {tot['files']:,}")
        print(f"  directories: {tot['dirs']:,}")
        print(f"  disk (data): {human(tot['bytes'])}  ({tot['bytes']:,} bytes)")
        print(f"  avg/account: {tot['files']//args.accounts:,} files")
        return 0

    if os.path.isdir(root) and os.listdir(root) and not args.force:
        print(f"root {root} is not empty; pass --force to write into it", file=sys.stderr); return 2
    os.makedirs(root, exist_ok=True)

    t0 = time.time()
    gen_tasks = [t + (root, args.fsync, args.chown) for t in base_tasks]
    accounts = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(_generate_account, gen_tasks, chunksize=2):
            accounts.append(r)

    files = sum(a["counts"]["files"] for a in accounts)
    nbytes = sum(a["counts"]["bytes"] for a in accounts)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "seed": args.seed, "layout": args.layout, "root": root,
        "generated_at": time.time(),
        "totals": {"accounts": len(accounts), "files": files, "bytes": nbytes},
        "accounts": accounts,
    }
    with open(args.manifest, "w") as fh:
        json.dump(manifest, fh, indent=2)

    dt = time.time() - t0
    print(f"wrote {files:,} files ({human(nbytes)}) across {len(accounts)} accounts "
          f"in {dt:.1f}s -> {root}")
    print(f"manifest: {args.manifest}")
    return 0


def _clean(manifest_path: str, root: str) -> int:
    if not os.path.exists(manifest_path):
        print(f"no manifest at {manifest_path}; refusing to clean", file=sys.stderr); return 2
    man = json.load(open(manifest_path))
    mroot = os.path.abspath(man.get("root", root))
    removed = 0
    for acc in man.get("accounts", []):
        adir = os.path.join(mroot, acc["id"])
        if os.path.commonpath([mroot, os.path.abspath(adir)]) != mroot or os.path.abspath(adir) == mroot:
            print(f"skip unsafe path {adir}", file=sys.stderr); continue
        if os.path.isdir(adir):
            shutil.rmtree(adir); removed += 1
    print(f"removed {removed} account trees under {mroot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
