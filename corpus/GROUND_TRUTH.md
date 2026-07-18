# GROUND_TRUTH — expected terminal states per corpus slice

Reference answers for evaluating the multi-agent pipeline. Not part of any
patient chart; never index this file into `chart_search`.

## The clinical story (demo-script aligned)

Daniel Okafor is an ESTABLISHED biologic patient: adalimumab 40 mg q2wk since
2025-09-14, well controlled by winter. The expensive decision at V4 is
"is the drug working, or do we switch and file a new prior authorization?"

- V1 2026-02-02 — routine biologic follow-up, well controlled: PASI 6.1,
  BSA 8%, IGA 2, DLQI 7.
- V2 2026-03-16 — stable: PASI 6.3, "no new plaques", DLQI 8. Med rec that
  day shows adalimumab last filled 2026-03-16 (true at the time).
- V3 2026-05-10 — flare "over the past several weeks": PASI 14.2, BSA 15%,
  IGA 3, DLQI 15, CRP 9, ESR 24. Onycholysis + new scalp plaques documented
  ONLY in the nursing note (the buried-finding beat is unchanged). Patient
  SAYS he continues his injections but is vague on dates.
- V4 2026-06-29 — worse again: PASI 16.8, BSA 18%, IGA 3, DLQI 17. The derm
  note concludes secondary loss of biologic response and plans to SWITCH
  biologics and file prior authorization — this is Agent A's chart-faithful
  recommendation. Adherence detail is deferred to a review that is NOT in
  the chart.

**The fact outside the chart** (`data/pharmacy/patient1.json`): last
adalimumab dispense `MD-004` handed over 2026-03-16 with a 28-day supply →
biologic coverage exhausted **2026-04-13**; NO adalimumab fill afterward.
Every documented data point of the flare (V3 2026-05-10, portal 2026-06-25,
V4 2026-06-29) postdates coverage exhaustion — the flare happened entirely
off-drug. Clobetasol refills continue (`MD-005`, 2026-06-15), so the biologic
gap is specific, not a general lapse. The chart alone cannot see any of this;
Agent B's pharmacy_lookup is the only path to it.

## Scenario A — `corpus/patient1/` (full chart) → `INSUFFICIENT_EVIDENCE`

Items 1-3 genuinely worsen (scores, symptoms incl. buried nail/scalp
findings, DLQI). But the "drug failure → switch" attribution fails: B's
dispense check shows the worsening interval is exactly the coverage gap, and
nothing in the chart establishes worsening before 2026-04-13 (V2 on
2026-03-16 was stable at PASI 6.3). Non-adherence is a live, unexcluded
confounder → the switch decision cannot be supported.

**Expected terminal: `INSUFFICIENT_EVIDENCE`** — with a to-do of resumed,
confirmed adherence and reassessment, NOT a biologic switch. A run that
outputs `CONFIRMED_WORSENING` on patient1 has failed to weigh the dispense
gap.

## Scenario B — `corpus/patient1_twin/` → `CONFIRMED_WORSENING`

Identical chart story, but the twin's pharmacy feed
(`data/pharmacy/patient1_twin.json`) shows fills continuing on schedule
(`MD-006` 2026-04-13, `MD-007` 2026-05-11, `MD-008` 2026-06-08 → coverage
through the visit), and the twin's chart includes an adherence-review note
confirming no gaps. When Agent B checks the dispense history it must find NO
gap and raise NO adherence challenge — standing down after checking is the
expected beat. This is TRUE secondary drug failure: the switch + prior auth
is justified.

**Expected terminal: `CONFIRMED_WORSENING`.**

## Scenario C — V1-V2 slice only → `STABLE`

PASI 6.1 → 6.3, BSA flat, IGA flat, DLQI 7 → 8, "no new plaques", well
controlled on current therapy. **Expected terminal: `STABLE`.**

## Dose math (single source of truth; test_pharmacy.py asserts this)

patient1: last fill 2026-03-16 + 28 days supply → coverage end 2026-04-13;
visit 2026-06-29 → 77 days without drug → 6 missed q2wk doses (due 04-13,
04-27, 05-11, 05-25, 06-08, 06-22 on the q2wk schedule).
twin: last fill 2026-06-08 + 28 days → coverage through 2026-07-06 ≥ visit.
