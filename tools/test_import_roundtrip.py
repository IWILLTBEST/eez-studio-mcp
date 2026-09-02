"""Round-trip acceptance for the reverse importer (eez2ir).

Chain under test, end-to-end through the real CLI (side-cars included):
    src.uixml ──compile──▶ proj.eez-project + ir_meta.json + translations.yaml
        ──eez2ir──▶ ir_b ──ir_to_xml──▶ out.uixml ──xml_to_ir──▶ ir_c
        ──Compiler──▶ C2
Oracles:
    1. canonical_diff(C1_from_disk, C2) == []   — the import loses NOTHING
    2. roundtrip_equal(ir_b, ir_c) == []        — the XML layer is lossless

反编译器往返验收：金标工程全部通过 = Studio 手改可无损回流 uixml。
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import uixml                      # noqa: E402
import eez2ir                     # noqa: E402
from ir2eez import Compiler       # noqa: E402

SRC_TARGETS = [
    ("glass", "examples/glass/glass.uixml"),
    ("i18n", "examples/i18n/i18n.uixml"),
    ("motor", "examples/motor/motor.uixml"),
    ("richdata", "examples/richdata/richdata.uixml"),
]


def main() -> int:
    fails = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, rel in SRC_TARGETS:
            src = os.path.join(ROOT, rel)
            work = os.path.join(tmp, name)
            os.makedirs(work)
            proj = os.path.join(work, f"{name}.eez-project")

            r = subprocess.run(
                [sys.executable, os.path.join(ROOT, "ir2eez.py"), src, "-o", proj],
                capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
            if r.returncode != 0:
                print(f"✗ {name}: compile failed\n{r.stdout}\n{r.stderr}")
                fails += 1
                continue

            with open(proj, encoding="utf-8") as f:
                c1 = json.load(f)

            meta, translations = eez2ir.load_sidecars(proj)
            try:
                ir_b = eez2ir.eez_to_ir(c1, meta, translations)
            except eez2ir.EEZImportError as e:
                print(f"✗ {name}: import failed: {e}")
                fails += 1
                continue

            out_uixml = os.path.join(work, f"{name}.uixml")
            uixml.ir_to_xml(ir_b, out_uixml)
            try:
                ir_c = uixml.xml_to_ir(out_uixml)
            except uixml.UIXMLError as e:
                print(f"✗ {name}: re-parsing the imported uixml failed: {e}")
                fails += 1
                continue

            xml_diffs = uixml.roundtrip_equal(ir_b, ir_c)
            if xml_diffs:
                print(f"✗ {name}: XML layer lossy ({len(xml_diffs)}):")
                for d in xml_diffs[:10]:
                    print(f"    {d}")
                fails += 1
                continue

            try:
                c2 = Compiler(ir_c).compile()
            except Exception as e:  # IRError or anything from bad IR
                print(f"✗ {name}: recompile failed: {e}")
                fails += 1
                continue

            diffs = eez2ir.canonical_diff(c1, c2)
            if diffs:
                print(f"✗ {name}: round trip differs ({len(diffs)}):")
                for d in diffs[:15]:
                    print(f"    {d}")
                fails += 1
            else:
                print(f"✓ {name}: compile → import → xml → parse → recompile identical")
    print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILED'} "
          f"({len(SRC_TARGETS)} projects)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
