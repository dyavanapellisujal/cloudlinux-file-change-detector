#!/usr/bin/env python3
"""
Change-load generator — drives REAL on-disk changes into the seeded tree so the candidate's
detector has a live workload, and emits the ORACLE: a ground-truth NDJSON log of every relevant
change (and noise) it made. The oracle is the objective scoring instrument — compare the
candidate's submitted jobs against it for recall / precision / dedup / latency.

Changes are made with ordinary syscalls (write / rename / chmod / utime) so any detection
mechanism sees them. Deterministic under --seed. Mechanism-neutral. No real malware — the
"needle" is just a distinguished, tagged change.

See change-load-generator-spec.md.

  python3 load_generator.py --manifest seed-manifest.json --scenario steady --duration 30
  python3 load_generator.py --manifest seed-manifest.json --scenario incident --needles 1 \
      --scanner-pid 12345 --scanner-outage-at 3 --scanner-outage-ms 1500
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import threading
import time

REL_EXT = {"php": ".php", "js": ".js", "html": ".html", "htaccess": ".htaccess"}
_CHUNK = b"// changed %d\n" % 0


def _account_of(path: str) -> str:
    for seg in path.split("/"):
        if seg.startswith("u") and seg[1:].isdigit():
            return seg
    return "?"


class Oracle:
    def __init__(self, path: str, dry: bool):
        self.dry = dry
        self.seq = 0
        self.counts: dict[str, int] = {}
        self.needles = 0
        # The oracle is the answer key — REVIEWER-ONLY. It is written only when an explicit
        # --oracle path is given, so a candidate running the default never gets one beside them.
        self._fh = None if (dry or not path) else open(path, "w")

    def emit(self, op: str, cls: str, path: str, scenario: str, is_needle: bool = False,
             size: int | None = None) -> None:
        self.seq += 1
        self.counts[cls] = self.counts.get(cls, 0) + 1
        if is_needle:
            self.needles += 1
        rec = {"seq": self.seq, "ts": round(time.time(), 6), "op": op, "class": cls,
               "path": path, "is_needle": is_needle, "account": _account_of(path),
               "scenario": scenario}
        if size is not None:
            rec["size"] = size
        if self._fh:
            self._fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()


class LoadGen:
    def __init__(self, manifest: dict, oracle: Oracle, seed: int, dry: bool, needle_token: str):
        self.oracle = oracle
        self.dry = dry
        self.rng = random.Random(seed)
        self.needle_token = needle_token
        accts = manifest["accounts"]
        self.doc_roots = [r for a in accts for r in a.get("doc_roots", [])]
        self.relevant = [p for a in accts for p in a.get("watch_targets", [])]
        self.noise = [p for a in accts for p in a.get("noise_targets", [])]
        self.plusx = [p for a in accts for p in a.get("plusx_pool", [])]
        if not self.doc_roots:
            raise SystemExit("manifest has no doc_roots — generate a seed first")

    # ---- low-level real changes --------------------------------------- #
    def _write(self, path: str, data: bytes, mode: str = "ab") -> int:
        if self.dry:
            return len(data)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as fh:
            fh.write(data)
        return len(data)

    def _new_rel_path(self, needle: bool = False) -> str:
        root = self.rng.choice(self.doc_roots)
        ext = self.rng.choice(list(REL_EXT.values()))
        tag = f"{self.needle_token}_" if needle else "inj_"
        return f"{root}/wp-content/uploads/2025/06/{tag}{self.oracle.seq + 1:06d}{ext}"

    # ---- relevant ops -------------------------------------------------- #
    def op_create(self, scenario: str, needle: bool = False) -> None:
        path = self._new_rel_path(needle)
        size = self._write(path, b"<?php /* injected */ ?>\n", "wb")
        self.oracle.emit("created", "relevant", path, scenario, is_needle=needle, size=size)

    def op_modify(self, scenario: str) -> None:
        if not self.relevant:
            return self.op_create(scenario)
        path = self.rng.choice(self.relevant)
        size = self._write(path, b"\n/* appended %d */\n" % self.oracle.seq, "ab")
        self.oracle.emit("modified", "relevant", path, scenario, size=size)

    def op_replace(self, scenario: str) -> None:
        if not self.relevant:
            return self.op_create(scenario)
        path = self.rng.choice(self.relevant)
        if not self.dry:
            tmp = f"{path}.tmp{self.oracle.seq}"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(b"<?php /* replaced */ ?>\n")
            os.replace(tmp, path)
        self.oracle.emit("replaced", "relevant", path, scenario, size=24)

    def op_plusx(self, scenario: str) -> None:
        if not self.plusx:
            return self.op_modify(scenario)
        path = self.rng.choice(self.plusx)
        if not self.dry and os.path.exists(path):
            st = os.stat(path); os.chmod(path, st.st_mode | 0o111)
        self.oracle.emit("chmod-x", "relevant", path, scenario)

    def op_relevant(self, scenario: str) -> None:
        self.rng.choices(
            [self.op_create, self.op_modify, self.op_replace, self.op_plusx],
            weights=[30, 45, 20, 5])[0](scenario)

    # ---- noise (must NOT become jobs) --------------------------------- #
    def op_noise(self, scenario: str) -> None:
        r = self.rng.random()
        root = self.rng.choice(self.doc_roots)
        if r < 0.35:
            p = f"{root}/logs/error.log"; self._write(p, b"x" * 64, "ab")
            self.oracle.emit("write-log", "noise", p, scenario)
        elif r < 0.6:
            p = f"{root}/wp-content/cache/c_{self.oracle.seq}.tmp"; self._write(p, b"tmp", "wb")
            self.oracle.emit("write-cache", "noise", p, scenario)
        elif r < 0.8 and self.relevant:
            p = self.rng.choice(self.relevant)               # metadata-only: atime touch
            if not self.dry and os.path.exists(p):
                st = os.stat(p); os.utime(p, (time.time(), st.st_mtime))
            self.oracle.emit("atime", "noise", p, scenario)
        else:
            p = f"{root}/wp-content/uploads/.draft_{self.oracle.seq}.swp"; self._write(p, b".", "wb")
            self.oracle.emit("write-swap", "noise", p, scenario)

    # ---- scenarios ----------------------------------------------------- #
    def steady(self, duration: float, rate_per_min: float, noise_ratio: float) -> None:
        n_rel = max(1, int(rate_per_min * duration / 60))
        interval = duration / n_rel if n_rel else duration
        for _ in range(n_rel):
            self.op_relevant("steady")
            for _ in range(int(noise_ratio)):
                self.op_noise("steady")
            if not self.dry:
                time.sleep(max(0.0, interval * self.rng.uniform(0.5, 1.5)))

    def burst(self, size: int, duration: float, needles: int, scenario: str = "burst") -> None:
        positions = set(self.rng.sample(range(size), min(needles, size))) if needles else set()
        per_sleep = (duration / size) if (size and not self.dry) else 0
        for i in range(size):
            if i in positions:
                self.op_create(scenario, needle=True)
            else:
                self.op_relevant(scenario)
            if per_sleep and i % 200 == 0:
                time.sleep(per_sleep * 200)

    def dedup_stress(self, window_ms: int, repeats: int) -> None:
        pool = self.relevant[:5] or [self._new_rel_path() for _ in range(5)]
        gap = (window_ms / 1000.0) / max(1, repeats)
        for _ in range(repeats):
            for path in pool:
                size = self._write(path, b"\n/* rewrite */\n", "ab")
                self.oracle.emit("modified", "relevant", path, "dedup-stress", size=size)
            if not self.dry:
                time.sleep(gap)

    def idle(self, duration: float, noise_ratio: float) -> None:
        n = max(1, int(10 * noise_ratio))
        for _ in range(n):
            self.op_noise("idle")


# --------------------------------------------------------------------------- #
def _schedule_outage(pid: int, at: float, ms: int) -> threading.Thread | None:
    if not pid or not at:
        return None

    def run():
        time.sleep(at)
        try:
            os.kill(pid, signal.SIGUSR1)            # option A: drive the stub outage
            time.sleep(ms / 1000.0)
            os.kill(pid, signal.SIGUSR1)            # recover
        except ProcessLookupError:
            pass
    t = threading.Thread(target=run, daemon=True); t.start(); return t


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Change-load generator (challenge fixture).")
    p.add_argument("--manifest", required=True)
    p.add_argument("--scenario", choices=["steady", "burst", "incident", "dedup-stress", "idle"],
                   default="steady")
    p.add_argument("--steady-rate", type=float, default=200, help="relevant changes/minute")
    p.add_argument("--noise-ratio", type=float, default=5.0)
    p.add_argument("--burst-size", type=int, default=50000)
    p.add_argument("--burst-duration", type=float, default=5.0)
    p.add_argument("--needles", type=int, default=1)
    p.add_argument("--needle-token", default="evil")
    p.add_argument("--dedup-window-ms", type=int, default=2000)
    p.add_argument("--dedup-repeats", type=int, default=10)
    p.add_argument("--scanner-pid", type=int, default=0)
    p.add_argument("--scanner-outage-at", type=float, default=0)
    p.add_argument("--scanner-outage-ms", type=int, default=1500)
    p.add_argument("--duration", type=float, default=30)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--oracle", default="", help="REVIEWER-ONLY: write the ground-truth oracle to this path (off by default)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    manifest = json.load(open(args.manifest))
    ver = manifest.get("schema_version")
    if ver not in (1, None):
        print(f"warning: seed manifest schema_version={ver} unrecognised (expected 1); "
              f"the contract may have drifted", file=sys.stderr)
    oracle = Oracle(args.oracle, args.dry_run)
    gen = LoadGen(manifest, oracle, args.seed, args.dry_run, args.needle_token)

    if args.scenario == "incident":
        _schedule_outage(args.scanner_pid, args.scanner_outage_at, args.scanner_outage_ms)

    t0 = time.time()
    if args.scenario == "steady":
        gen.steady(args.duration, args.steady_rate, args.noise_ratio)
    elif args.scenario == "burst":
        gen.burst(args.burst_size, args.burst_duration, args.needles)
    elif args.scenario == "incident":
        gen.steady(min(3.0, args.duration / 3), args.steady_rate, args.noise_ratio)
        gen.burst(args.burst_size, args.burst_duration, args.needles, scenario="incident")
        gen.steady(min(3.0, args.duration / 3), args.steady_rate, args.noise_ratio)
    elif args.scenario == "dedup-stress":
        gen.dedup_stress(args.dedup_window_ms, args.dedup_repeats)
    elif args.scenario == "idle":
        gen.idle(args.duration, args.noise_ratio)
    oracle.close()

    rel = oracle.counts.get("relevant", 0); noise = oracle.counts.get("noise", 0)
    print(f"{'DRY RUN — ' if args.dry_run else ''}scenario={args.scenario} "
          f"ops={oracle.seq} relevant={rel} noise={noise} needles={oracle.needles} "
          f"in {time.time() - t0:.1f}s")
    if args.oracle and not args.dry_run:
        print(f"oracle (reviewer): {args.oracle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
