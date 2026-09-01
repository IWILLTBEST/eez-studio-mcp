#!/usr/bin/env python3
"""Headless-CI orchestrator: compile every example IR, drive the running EEZ
Studio over the bridge, run check + visual-golden comparison per screen.

Expects a Studio instance with the bridge alive (EEZ_BRIDGE_URL, default
http://127.0.0.1:17620). Exit 0 only if every step passes.

Usage: python tools/ci-check.py [--pct 1.0]
  --pct: golden tolerance for CI (default 1.0 — catches gross regressions;
         strict 0.1 stays for local dev where rendering is bit-stable)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import visreg  # noqa: E402  (reuse capture/diff/golden plumbing)

# (example dir, IR basename, make script or None, [(screen, golden, loose), ...])
# loose = single-shot capture (screen contains a continuously animating element,
# e.g. the lv_calendar header label auto-scrolls when the text does not fit).
EXAMPLES = [
    ("glass", "glass.ir.json", "make_glass.py", [("main", "glass", False)]),
    ("i18n", "i18n.ir.json", "make_i18n.py", [("main", "i18n", False)]),
    ("motor", "motor.ir.json", None, [("overview", "motor", False)]),
    ("richdata", "richdata.ir.json", "make_richdata.py", [
        ("main", "richdata", False),
        ("controls", "richdata-controls", True),
        ("settings", "richdata-settings", False),
    ]),
]

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str) -> None:
    results.append((name, ok, note))
    print(f"{'✓' if ok else '✗'} {name}: {note}", flush=True)


def wait_bridge(seconds: int = 120) -> bool:
    for _ in range(seconds // 3):
        try:
            with urllib.request.urlopen(
                visreg.BRIDGE.replace("/tool", "/health"), timeout=3
            ) as r:
                if json.loads(r.read().decode()).get("ok"):
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pct", type=float, default=1.0)
    ap.add_argument("--baseline", action="store_true",
                    help="re-capture goldens with the CURRENT public toolchain fonts "
                         "(the public repo ships rel_* free substitute fonts — glyph "
                         "bitmaps differ from the private msyh-derived ones, so "
                         "goldens must be captured with the public font chain)")
    args = ap.parse_args()

    if not wait_bridge():
        record("bridge", False, f"not reachable at {visreg.BRIDGE} — is Studio running?")
        return 1
    record("bridge", True, visreg.BRIDGE)

    for ex, ir, make, screens in EXAMPLES:
        exdir = os.path.join(ROOT, "examples", ex)
        if make:
            r = run([sys.executable, os.path.join(exdir, make)], exdir)
            record(f"{ex}/make", r.returncode == 0, (r.stderr or r.stdout)[-120:].strip())
        project = os.path.join(exdir, ir.replace(".ir.json", ".eez-project"))
        r = run([sys.executable, os.path.join(ROOT, "ir2eez.py"),
                 os.path.join(exdir, ir), "-o", project], ROOT)
        ok = r.returncode == 0 and "✗" not in (r.stdout + r.stderr)
        record(f"{ex}/compile", ok, (r.stderr or r.stdout).strip().splitlines()[-1][:100] if (r.stderr or r.stdout).strip() else "ok")
        if not ok:
            continue

        # check 0/0
        try:
            c = visreg.call("check", {})
            errs = c.get("numErrors", 99)
            warns = c.get("numWarnings", 99)
            record(f"{ex}/check", errs == 0 and warns == 0, f"{errs} errors, {warns} warnings")
        except Exception as e:
            record(f"{ex}/check", False, str(e)[:100])

        for screen, golden, loose in screens:
            try:
                png = visreg.capture(project, screen, stabilize=not loose)
                img = visreg.load_png(png)
                golden_path = os.path.join(visreg.GOLDEN_DIR, f"{golden}.png")
                if args.baseline:
                    img.save(golden_path)
                    meta = {"name": golden, "project": project, "screen": screen,
                            "capturedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "size": list(img.size)}
                    json.dump(meta, open(golden_path[:-4] + ".json", "w", encoding="utf-8"), indent=1)
                    record(golden, True, "baseline re-captured (public fonts)")
                    continue
                if not os.path.exists(golden_path):
                    record(golden, False, "golden missing in golden/")
                    continue
                golden_img = visreg.load_png(open(golden_path, "rb").read())
                res = visreg.diff_images(golden_img, img, args.delta if hasattr(args, "delta") else 12)
                if not res[0].get("sameSize"):
                    record(golden, False, f"size {res[0].get('sizeA')} vs {res[0].get('sizeB')}")
                    continue
                metrics, diff_img = res
                ok = metrics["changedPct"] <= args.pct
                img.save(os.path.join(visreg.GOLDEN_DIR, f"{golden}.last.png"))
                diff_img.save(os.path.join(visreg.GOLDEN_DIR, f"{golden}.diff.png"))
                record(golden, ok,
                       f"{metrics['changedPct']}% changed (tol {args.pct}%), "
                       f"bbox={metrics.get('bbox')}")
            except Exception as e:
                record(golden, False, str(e)[:120])

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} steps passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
