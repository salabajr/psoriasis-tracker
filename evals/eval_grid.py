"""3-row eval grid (BUILD.md Phase 2 step 6).

Rows 1-2 assert the persisted terminal states of the most recent live runs
against corpus/GROUND_TRUTH.md. Row 3 asserts verify_quote rejects an
injected fake quote. No API calls.

Run: .venv/bin/python evals/eval_grid.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import verify_quote  # noqa: E402

ROWS = [
    ("patient1 full chart", "runs/patient1_final.json", "INSUFFICIENT_EVIDENCE"),
    ("patient1_twin (adherent)", "runs/patient1_twin_final.json", "CONFIRMED_WORSENING"),
]

failures = 0
for label, path, expected in ROWS:
    full = os.path.join(ROOT, path)
    if not os.path.exists(full):
        print(f"SKIP  {label}: {path} not found (run main.py first)")
        failures += 1
        continue
    got = json.load(open(full))["terminal_state"]
    ok = got == expected
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}: terminal {got} (expected {expected})")

fake = "PASI 99.9, BSA 90%, IGA 5"
rejected = not verify_quote("visit3_derm_note", fake, os.path.join(ROOT, "corpus/patient1"))
real = verify_quote("visit3_derm_note", "PASI 14.2, BSA 15%, IGA 3", os.path.join(ROOT, "corpus/patient1"))
ok = rejected and real
failures += 0 if ok else 1
print(f"{'PASS' if ok else 'FAIL'}  verify_quote: rejects injected fake quote, accepts the real one")

print("EVAL GRID:", "ALL PASS" if failures == 0 else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
