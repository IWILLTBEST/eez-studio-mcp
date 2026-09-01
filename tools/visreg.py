#!/usr/bin/env python3
"""Visual regression harness for EEZ Studio LVGL projects.

Drives the EEZ bridge (reload -> navigate -> screenshot), stores golden
baselines and pixel-compares new captures against them.

Usage:
  python tools/visreg.py baseline --name glass --project PATH [--screen main]
  python tools/visreg.py check   --name glass --project PATH [--screen main]
      [--pct 0.1] [--delta 12]
  python tools/visreg.py list

Exit codes (check): 0 = match, 1 = diff over tolerance, 2 = error.
JSON summary is the last stdout line; diff image at golden/<name>.diff.png.

Bridge: EEZ_BRIDGE_URL (default http://127.0.0.1:17620/tool — the built-in
fork bridge; the extension bridge on 17621 speaks the same protocol).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "golden")


def _bridge_url() -> str:
    """EEZ_BRIDGE_URL may be host-only (the MCP server convention) or a full
    endpoint; normalize to the .../tool route."""
    url = os.environ.get("EEZ_BRIDGE_URL", "http://127.0.0.1:17620")
    if not url.rstrip("/").endswith("/tool"):
        url = url.rstrip("/") + "/tool"
    return url


BRIDGE = _bridge_url()


def call(tool: str, args: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        BRIDGE,
        data=json.dumps({"tool": tool, "args": args}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{tool}: HTTP {e.code} {e.read().decode()[:200]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"{tool}: bridge unreachable at {BRIDGE} — is EEZ Studio running?"
        ) from e
    if not out.get("ok"):
        raise RuntimeError(f"{tool}: {json.dumps(out)[:200]}")
    return out.get("result", {})


def _shot() -> bytes:
    data_url = call("screenshot", {}).get("dataUrl", "")
    if not data_url.startswith("data:image/png;base64,"):
        raise RuntimeError("screenshot: unexpected payload")
    return base64.b64decode(data_url.split(",", 1)[1])


def capture(project: str, screen: str, stabilize: bool = True) -> bytes:
    """Fresh screenshot of a project page: reopen from disk, then screenshot.
    Paths are normalized to forward slashes (tab identity in the bridge is
    string-based; mixed separators open duplicate tabs). open_project/reload
    retry — a bridge busy loading another tab answers transient "not found".
    stabilize=True waits for two identical consecutive frames (paint settled);
    screens with a continuously animating element (e.g. the lv_calendar header
    label auto-scrolling) must pass stabilize=False."""
    project = os.path.abspath(project).replace("\\", "/")
    for attempt in range(3):
        try:
            call("open_project", {"path": project})
            break
        except RuntimeError:
            if attempt == 2:
                raise
            time.sleep(4)
    call("reload", {})
    time.sleep(3.5)
    call("navigate", {"screen": screen, "object": f"screen_{screen}"})
    time.sleep(1.5)
    prev = _shot()
    if not stabilize:
        return prev
    for _ in range(6):
        time.sleep(0.8)
        cur = _shot()
        if prev == cur:
            return cur
        prev = cur
    raise RuntimeError("canvas did not stabilize after reload (6 tries)")


def load_png(data: bytes):
    from PIL import Image
    import io
    return Image.open(io.BytesIO(data)).convert("RGB")


def diff_images(a, b, delta_thresh: int):
    """Return metrics + annotated diff. Pixels count as changed when any channel
    moves more than delta_thresh (tolerates font anti-aliasing jitter)."""
    import numpy as np
    A, B = np.asarray(a, dtype=int), np.asarray(b, dtype=int)
    if A.shape != B.shape:
        return {"sameSize": False, "sizeA": list(a.size), "sizeB": list(b.size)}
    d = np.abs(A - B)
    changed = (d > delta_thresh).any(axis=2)
    count = int(changed.sum())
    total = changed.size
    metrics = {
        "sameSize": True,
        "size": list(a.size),
        "changedPixels": count,
        "changedPct": round(100.0 * count / total, 4),
        "maxDelta": int(d.max()),
    }
    if count:
        ys, xs = np.where(changed)
        metrics["bbox"] = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
    diff_img = a.copy()
    if count:
        from PIL import Image as _PILImage
        arr = np.asarray(diff_img).copy()
        arr[changed] = [255, 0, 0]
        diff_img = _PILImage.fromarray(arr)
    return metrics, diff_img


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["baseline", "check", "list"])
    ap.add_argument("--name", help="golden name (project page identifier)")
    ap.add_argument("--project", help="path to the .eez-project")
    ap.add_argument("--screen", default="main", help="page name (default main)")
    ap.add_argument("--pct", type=float, default=0.1,
                    help="fail when changed pixels exceed this %% (default 0.1)")
    ap.add_argument("--delta", type=int, default=12,
                    help="per-channel pixel tolerance, anti-aliasing (default 12)")
    args = ap.parse_args()

    os.makedirs(GOLDEN_DIR, exist_ok=True)

    if args.cmd == "list":
        for f in sorted(os.listdir(GOLDEN_DIR)):
            if f.endswith(".png"):
                meta_p = os.path.join(GOLDEN_DIR, f[:-4] + ".json")
                meta = json.load(open(meta_p, encoding="utf-8")) if os.path.exists(meta_p) else {}
                print(f"{f[:-4]:24} {meta.get('capturedAt', '?'):22} {meta.get('project', '?')}")
        return 0

    if not args.name or not args.project:
        ap.error("baseline/check need --name and --project")

    png = capture(args.project, args.screen)
    img = load_png(png)
    golden_png = os.path.join(GOLDEN_DIR, f"{args.name}.png")
    meta = {
        "name": args.name,
        "project": os.path.abspath(args.project),
        "screen": args.screen,
        "capturedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "size": list(img.size),
    }

    if args.cmd == "baseline":
        img.save(golden_png)
        json.dump(meta, open(golden_png[:-4] + ".json", "w", encoding="utf-8"), indent=1)
        print(json.dumps({"ok": True, "saved": golden_png, **meta}))
        return 0

    if not os.path.exists(golden_png):
        print(json.dumps({"ok": False, "error": f"no golden for {args.name!r} — run baseline first"}))
        return 2
    golden = load_png(open(golden_png, "rb").read())
    result = diff_images(golden, img, args.delta)
    if not result[0].get("sameSize"):
        print(json.dumps({"ok": False, "name": args.name, **result[0]}))
        return 1
    metrics, diff_img = result
    img.save(os.path.join(GOLDEN_DIR, f"{args.name}.last.png"))
    diff_img.save(os.path.join(GOLDEN_DIR, f"{args.name}.diff.png"))
    passed = metrics["changedPct"] <= args.pct
    print(json.dumps({
        "ok": passed, "name": args.name, "tolerancePct": args.pct,
        "delta": args.delta, **metrics}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(2)
