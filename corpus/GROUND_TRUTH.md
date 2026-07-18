# GROUND_TRUTH — expected terminal states per corpus slice

Reference answers for evaluating the multi-agent pipeline. Not part of any
patient chart; never index this file into `chart_search`.

## Scenario A — `corpus/patient1/` (full chart) → `INSUFFICIENT_EVIDENCE`

The chart shows genuine directional worsening:

- Rubric 1 (objective trend): PASI 6.1 → 6.3 → 14.2 → 16.8; BSA 8% → 15% → 18%; IGA 2 → 3.
- Rubric 2 (symptom trajectory): onycholysis on two fingernails and new scalp
  plaques along the posterior hairline — documented ONLY in
  `visit3_nursing_note`, deliberately absent from every derm note. A pipeline
  that only reads derm notes should miss this and be challenged on it
  (searching "onycholysis" after "nail" terms fail is the intended
  `search_trace` behavior).
- Rubric 3 (DLQI trend): 7 → 8 → 15 → 17.

However, rubrics 4 and 5 cannot pass on the full chart:

- Rubric 4 (adequate, adherent trial): `visit4_adherence_note` documents
  "doses taken 2026-05-10 and 2026-05-24; doses due 2026-06-07 and 2026-06-21
  were missed" — two missed adalimumab doses plus a 3-week NB-UVB gap. The
  biologic trial is neither adequate nor adherent at the time of the V4 scores.
- Rubric 5 (confounders addressed): non-adherence is a live, unaddressed
  confounder. Critically, the chart is intentionally ambiguous about ordering:
  the portal message (2026-06-25) says only "worse over the past few weeks",
  which cannot be pinned before or after the first missed dose (due
  2026-06-07). No document states a causal or temporal link in either
  direction. Therefore worsening cannot be attributed to disease progression.

**Expected terminal state: `INSUFFICIENT_EVIDENCE`** — worsening is real on
items 1-3 but confounded by non-adherence, and the chart cannot establish that
worsening preceded the missed doses. A run that outputs `CONFIRMED_WORSENING`
on the full chart has failed to surface or weigh the confounder.

## Scenario B — `corpus/patient1_twin/` (adherence-clean twin) → `CONFIRMED_WORSENING`

Identical chart except the adherence note instead documents: adherence
confirmed, no gaps, no interruptions (all adalimumab doses taken on schedule
2026-05-10, 2026-05-24, 2026-06-07, 2026-06-21; NB-UVB attendance unbroken at
3x/wk). With the confounder removed, rubric 4 passes (adequate adherent trial)
and rubric 5 passes (non-adherence excluded; no infection, steroid withdrawal,
or seasonal pattern documented), while rubrics 1-3 still show worsening.

**Expected terminal state: `CONFIRMED_WORSENING`.**

## Scenario C — V1-V2 slice only → `STABLE`

Corpus restricted to `visit1_derm_note`, `visit2_derm_note`, `visit2_med_list`:
PASI 6.1 → 6.3 (below any meaningful-change threshold), BSA flat at 8%, IGA
flat at 2, DLQI 7 → 8, and the note states "no new plaques, tolerating
phototherapy". No confounders in play; regimen unchanged and tolerated.

**Expected terminal state: `STABLE`.**

## Twin scaffolding (for the Phase-2 lead)

To build `corpus/patient1_twin/`:

1. Copy `corpus/patient1/` to `corpus/patient1_twin/` in full.
2. Swap exactly ONE file: replace `visit4_adherence_note.md` with a version
   whose dose accounting reads adherence confirmed, no gaps, no interruptions —
   all four adalimumab doses taken on their due dates (2026-05-10, 2026-05-24,
   2026-06-07, 2026-06-21) and NB-UVB attendance continuous at 3x/wk with no
   gap. Keep the same filename, header block, author, and 2026-06-29 date so
   `doc_id` (`visit4_adherence_note`) is identical in both corpora.
3. Change nothing else. All other files (including the portal message and both
   V4 notes) are byte-identical between `patient1` and `patient1_twin`; the
   terminal-state flip must be attributable solely to the adherence document.
