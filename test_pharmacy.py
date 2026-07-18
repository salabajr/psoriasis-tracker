"""Pharmacy verification feature — deterministic checks (no API calls).

1. Consistency: bundle dates + daysSupply reproduce the corpus dose math —
   patient1 coverage ends 2026-06-07, two missed doses by the 2026-06-29
   visit; twin coverage is continuous through the visit. Single source of
   truth: change a date anywhere and this fails.
2. verify_dispense grounding: real IDs pass, MD-999 is rejected.
3. Adversarial: a challenge citing MD-999 has that evidence dropped without
   crashing (agents._validate_external_evidence).
"""
from datetime import date, timedelta

import agents
import tools

VISIT_DATE = date(2026, 6, 29)
DOSE_INTERVAL_DAYS = 14

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print("PASS ", msg)
    else:
        failed += 1
        print("FAIL ", msg)


def adalimumab_dispenses(patient_id):
    bundle = tools.pharmacy_lookup(patient_id)
    out = []
    for entry in bundle["entry"]:
        r = entry["resource"]
        if "adalimumab" in r["medicationCodeableConcept"]["text"].lower():
            out.append(r)
    return sorted(out, key=lambda r: r["whenHandedOver"])


def coverage_end(dispenses):
    last = dispenses[-1]
    y, m, d = map(int, last["whenHandedOver"].split("-"))
    return date(y, m, d) + timedelta(days=int(last["daysSupply"]["value"]))


def missed_doses(dispenses, visit):
    first = dispenses[0]
    y, m, d = map(int, first["whenHandedOver"].split("-"))
    due = date(y, m, d)
    end = coverage_end(dispenses)
    missed = 0
    while due < visit:
        if due >= end:
            missed += 1
        due += timedelta(days=DOSE_INTERVAL_DAYS)
    return missed


# --- 1. Consistency: dispense math reproduces the demo-script story --------
# Established adalimumab patient; last fill 2026-03-16 + 28d supply ->
# coverage exhausted 2026-04-13; no fill afterward; every documented flare
# data point (V3 2026-05-10, portal 2026-06-25, V4 2026-06-29) is off-drug.
p1 = adalimumab_dispenses("patient1")
check(len(p1) == 3 and p1[-1]["id"] == "MD-004",
      "patient1: three adalimumab fills, last is MD-004 (2026-03-16)")
check(coverage_end(p1) == date(2026, 4, 13),
      "patient1: coverage end 2026-04-13 (2026-03-16 + 28 days)")
check(missed_doses(p1, VISIT_DATE) == 6,
      "patient1: six missed q2wk doses by the 2026-06-29 visit")
check(coverage_end(p1) < date(2026, 5, 10),
      "patient1: coverage ended BEFORE the first documented flare (V3)")

tw = adalimumab_dispenses("patient1_twin")
check(tw[-1]["id"] == "MD-008" and len(tw) == 6,
      "twin: fills continue on schedule, last MD-008 (2026-06-08)")
check(coverage_end(tw) >= VISIT_DATE,
      "twin: coverage continuous through the 2026-06-29 visit")
check(missed_doses(tw, VISIT_DATE) == 0, "twin: zero missed doses")

n1 = len(tools.pharmacy_lookup("patient1")["entry"])
check(n1 == 5, "patient1 bundle returns 5 dispenses (incl. clobetasol noise)")

# --- 2. verify_dispense grounding ------------------------------------------
check(tools.verify_dispense("MD-004", "patient1"), "verify_dispense MD-004 ok")
check(not tools.verify_dispense("MD-999", "patient1"),
      "verify_dispense rejects MD-999")
check(not tools.verify_dispense("MD-008", "patient1"),
      "MD-008 (twin-only) rejected against patient1")

# --- 3. Adversarial: fake resource_id is dropped, run does not crash -------
challenge = {
    "criterion_id": 5,
    "challenge_reason": "adherence gap",
    "rubric_quote": "confounder",
    "what_would_satisfy": "evidence of adherence",
    "external_evidence": [
        {"resource_type": "MedicationDispense", "resource_id": "MD-999",
         "detail": "fabricated"},
        {"resource_type": "MedicationDispense", "resource_id": "MD-004",
         "detail": "last dispense 2026-03-16, 28-day supply"},
    ],
}
out, dropped = agents._validate_external_evidence([dict(challenge)], "patient1")
check(dropped == ["MD-999"], "MD-999 dropped by verify_dispense gate")
check([e["resource_id"] for e in out[0]["external_evidence"]] == ["MD-004"],
      "valid MD-004 evidence survives")

all_fake = dict(challenge)
all_fake["external_evidence"] = [{"resource_id": "MD-999", "detail": "x"}]
out2, dropped2 = agents._validate_external_evidence([all_fake], "patient1")
check("external_evidence" not in out2[0] and dropped2 == ["MD-999"],
      "all-fake evidence -> field removed, challenge text intact, no crash")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
