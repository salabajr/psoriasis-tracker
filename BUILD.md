# BUILD.md — Psoriasis Progression Monitor (autonomous build brief)

You are the lead coding agent. Build a two-agent adversarial disease-monitoring
tool. Time budget ~4h. Work in three phases: **Phase 0 you do alone and
serially** (it unblocks everyone); **Phase 1 you fan out to 5 parallel
subagents**; **Phase 2 you integrate serially**. Do not start Phase 1 until
Phase 0 is committed.

---

## PRE-FLIGHT (human does this before H0 — hard blocker for Phase 1 API work)

1. **Claim the $100 credits NOW.** Link:
   `https://claude.com/offers?offer_code=46b9a7ba-b27b-471c-b65b-f08e5e3673c8`
   — claim with the email on your Anthropic Console account. **The form closes
   Sun July 19 (tomorrow night); claim before building.** Credits valid 90 days.
2. **Set the key server-side only:** `export ANTHROPIC_API_KEY=...`. This repo is
   PUBLIC (rights-clean redistribution is a project requirement), so: key never
   in code, never committed. Add `.gitignore` (`.env`, `*.key`) and a
   `.env.example` in Phase 0. The UI makes no Claude calls, so no key ever
   touches `static/`.
3. **Install the build harness:** `npm install -g @anthropic-ai/claude-code`, log
   in with the Console account (Claude Code usage also draws on the $100 — the
   build agent and the app's runtime calls share the budget; prompt caching keeps
   the app side cheap).
4. **Smoke-test one call** (Get Started guide) to confirm key + credits + model
   string all resolve before fanning out.
5. **SDK decision (important):** the app's agents use the **plain `anthropic`
   Python SDK** with the hand-rolled orchestrator in this brief — NOT the Claude
   Agent SDK. The visible, deterministic challenge→repair loop IS the product; an
   agent-harness would abstract away the exact thing the demo must show. Claude
   Code (agentic) builds it; the shipped app stays raw-SDK and inspectable.
6. **If the first real call fails, check these three first** (the event's own POC
   says most tickets are auth/rate-limit): key exported in the subagent's env,
   credits actually claimed, `config.MODEL` resolves. Escalation: hackathon
   Slack / `hackathon+abridge-lsvp-hackathon@anthropic.com`.

---

## ORCHESTRATION PROTOCOL (read first)

1. **Phase 0 (you, serial, ~15 min).** Create the repo, write the frozen
   contracts and fixtures below, commit with message `freeze: contracts+fixtures`.
   Nothing here calls the Claude API.
2. **Phase 1 (spawn 5 subagents).** Give each subagent exactly the brief in its
   section. Enforce these invariants or the concurrency breaks:
   - **One writer per file.** Ownership is disjoint (table below). A subagent
     touches only its own files.
   - **Contracts and fixtures are READ-ONLY after Phase 0.** If a subagent
     believes a contract is wrong, it STOPS and reports to you — it must not
     edit a shared contract, because that silently breaks its siblings.
   - **Build against fixtures, never against a sibling's live output.** Every
     workstream has fixture inputs so it needs nothing from the others at
     runtime during Phase 1.
   - **Done = the workstream's acceptance check passes.** Subagent runs its own
     check before reporting done.

   **How the parallelism actually works (Claude Code reality):** standard
   subagents that WRITE files are serialized by the orchestrator — real
   concurrency is read-only. So treat the fan-out as: (a) **WS-CORPUS and WS-UI
   are truly parallel-safe now** — neither calls the API, neither writes shared
   code, so run them concurrently/background; (b) **WS-TOOLS, WS-AGENTS, WS-ORCH
   write code and will serialize** — the disjoint ownership is what lets you run
   them back-to-back with zero merge conflicts, and it's the precondition for
   fork mode if you want true write-parallelism (`CLAUDE_CODE_FORK_SUBAGENT=1`,
   Claude Code ≥ v2.1.117 — forks share the parent's prompt cache). Keep to
   these 5 (3–5 is the practical ceiling). **Scope each subagent's tools:**
   CORPUS and UI get filesystem only, NO API key (they never call Claude);
   TOOLS/AGENTS/ORCH get filesystem + Anthropic API. Write each subagent's
   dispatch description as a triage rule: "Use for X. Owns files Y. Returns Z."
3. **Phase 2 (you, serial).** Wire real components, run the five integration
   gates, build the twin patient, verify against ground truth, cache a run.

### File ownership (disjoint — enforce)

| Workstream | Owns (writes) | Reads (never writes) |
|---|---|---|
| WS0 lead / Phase 0 | `contracts.md`, `config.py`, `rubric.json`, `fixtures/**`, `README.md`, later `main.py` | — |
| WS-CORPUS | `corpus/**` | corpus sheet §C |
| WS-TOOLS | `tools.py` | `config.py`, `contracts.md`, `fixtures/corpus_tiny/**` |
| WS-AGENTS | `agents.py`, `prompts/**` | `config.py`, `contracts.md`, `fixtures/*.json` |
| WS-ORCH | `orchestrator.py` | `config.py`, `contracts.md`, `fixtures/*.json` |
| WS-UI | `app.py`, `static/**` | `contracts.md`, `fixtures/fixture_runstate.json` |

---

## NON-NEGOTIABLE INVARIANTS (five wiring fixes — bake in, do not rediscover)

These correct real bugs in the source spec. Every relevant workstream honors them.

1. **Agent B has no chart access and cannot cite chart facts.** B works from
   packet + rubric ONLY. A legal challenge is constructible from those two alone.
   The confounder beat has exactly two legal shapes, both ending in concession:
   - *Inconsistency:* packet item 1 = worsening while item 5 shows an unexcluded
     confounder → B challenges the contradiction.
   - *Absence:* item 5 is thin/unaddressed → B challenges "rubric forbids
     attributing worsening while a confounder is unaddressed."
   Any challenge that asserts a specific chart fact B could not have seen is a bug.
2. **The buried-finding reformulation happens in ASSEMBLE, not in a B round.**
   It surfaces in `search_trace`, not as a B challenge. B's one staged challenge
   is the confounder.
3. **The biologic starts at V3** (adalimumab 40 mg q2wk, first dose 2026-05-10).
   Without this, V4's "missed doses" and rubric item 4 have no referent.
4. **Terminal logic:** if any load-bearing item is `insufficient` (conceded)
   when B stands down, terminal = `INSUFFICIENT_EVIDENCE` — regardless of round
   count. Do NOT require burning all 3 rounds to reach it.
5. **No typo'd-date noise doc.** It induces an unstaged hygiene challenge. Skip it.

---

## CLAUDE BUILD PRACTICES (fold into WS-TOOLS and WS-AGENTS)

Reconciled with the event resources doc (read) + Anthropic's current agent
guidance. The event doc pins no specific model — confirm `config.MODEL` via the
model-comparison page / Console.

- **Credits + model.** ~$100 per participant, shared across the build agent and
  the app's runtime. The spec pins `claude-sonnet-4-6`; keep it in `config.py` as
  the ONLY place a model string appears. One model everywhere — model-mixing is a
  debugging tax you can't afford in 4h. (Credit-pressure fallback ONLY: the
  chart_search ranking call is the one safe place to drop to a cheaper model,
  since it's extraction not judgment. Don't unless credits actually run low.)
- **Prompt caching is the highest-ROI lever here.** Every `chart_search`,
  assemble, review, and repair call re-sends the same large static prefix (agent
  system prompt + rubric + the ~10–12-note corpus block). Mark that prefix with
  `cache_control: {"type": "ephemeral"}` so repeated calls read it at ~10% of the
  input rate instead of reprocessing it — ~90% cheaper and lower latency on a
  loop that hammers the same corpus. Structure every request as
  `[stable cached prefix] + [variable tail]`; never interleave the variable query
  into the cached block or the cache misses. TTL is ~5 min, which fits a live
  demo run; a run after a long idle gap just re-warms once. Log
  `cache_read_input_tokens` on the first integrated run to confirm hits.
- **Cost discipline.** verify_quote and the keyword prefilter in chart_search are
  plain Python — no API call. Only the ranking step of chart_search and the three
  agent modes call Claude. Keep it that way; don't add model calls to the tools.
- **Reference recipes (don't reinvent):** tool-use / function-calling shape for
  `agents.py` and `tools.py` → Claude Cookbooks tool-use notebook
  (github.com/anthropics/claude-cookbooks); the three system prompts → Prompt
  Engineering Interactive Tutorial + prompting best-practices docs; the eval-grid
  icing → Cookbooks evals recipe; the FHIR MCP icing → fork a reference server
  from github.com/modelcontextprotocol/servers rather than building from zero.

---

## PHASE 0 — CONTRACTS + FIXTURES (you, serial)

### Repo layout
```
.
├── BUILD.md  contracts.md  config.py  rubric.json  README.md  main.py
├── agents.py orchestrator.py tools.py
├── prompts/{assemble,repair,review}.txt
├── corpus/patient1/*.md   corpus/patient1_twin/*.md
├── static/index.html      app.py
├── fixtures/{fixture_runstate.json, fixture_packet.json, fixture_challenges.json, corpus_tiny/*.md}
└── runs/  evals/  LATER.md
```

### config.py
```python
MODEL = "claude-sonnet-4-6"   # spec-specified + ONLY place a model string lives.
                              # Confirm it resolves against the credit-linked
                              # account; if the event doc names another, use that.
TEMPERATURE = 0
MAX_ROUNDS = 3
CORPUS_DIR = "corpus/patient1"
CACHE_PREFIX = True           # mark system+rubric+corpus prefix with cache_control
```
Every Claude call imports MODEL/TEMPERATURE from here. No inline model strings.
When `CACHE_PREFIX`, the stable prefix carries `cache_control:{"type":"ephemeral"}`.

### contracts.md — freeze these verbatim

**Frozen function signatures** (WS-TOOLS/AGENTS/ORCH build to these exactly):
```python
# tools.py
def chart_search(query: str, corpus_dir: str) -> list[dict]:
    # -> [{"doc_id","quote","date","score"}], top ~5, quotes verbatim substrings
def rubric_lookup() -> list[dict]:            # returns rubric.json array
def verify_quote(doc_id: str, quote: str, corpus_dir: str) -> bool

# agents.py
def assemble(rubric: list[dict], corpus_dir: str) -> dict:   # -> run_state (round 0)
def review(packet: list[dict], rubric: list[dict]) -> list[dict]:  # -> challenges
def repair(packet: list[dict], challenges: list[dict], corpus_dir: str) -> list[dict]  # -> patched packet

# orchestrator.py
def run(patient_dir: str) -> dict:            # -> final run_state
```

**Frozen JSON schemas:**
```jsonc
// packet entry
{"criterion_id":1,"criterion_text":"...","status":"worsening|stable|improving|insufficient",
 "evidence":[{"doc_id":"...","quote":"...","date":"YYYY-MM-DD"}],
 "reasoning":"...","rubric_ref":"..."}
// challenge (B → A)
{"criterion_id":1,"challenge_reason":"...","rubric_quote":"...","what_would_satisfy":"..."}
// run_state (persisted every round → runs/, drives UI)
{"patient_id":"...","round":0,"terminal_state":null,
 "packet":[/*entries*/],"challenges":[/*per round*/],
 "search_trace":[{"criterion_id":2,"attempts":["nail involvement","onycholysis"],
                  "resolved_in":"visit3_nursing_note"}]}
```
`terminal_state ∈ {CONFIRMED_WORSENING, STABLE, INSUFFICIENT_EVIDENCE, null}`.

### rubric.json — write the 5 items (from spec §3.1)
Items: (1) objective severity trend PASI/BSA/IGA; (2) symptom trajectory
incl. nail/joint; (3) DLQI trend; (4) treatment-response adequacy; (5) confounder
check — text: *"Worsening cannot be attributed to disease if a confounder
(non-adherence, infection, steroid-withdrawal or seasonal flare) is unaddressed."*
Each item: `{"id","text","how"}`.

### Fixtures (this is what makes parallelism possible — write real content)
- `fixtures/fixture_runstate.json` — a COMPLETE hand-authored 2-round run for
  patient1 (round 2, terminal INSUFFICIENT_EVIDENCE, one search_trace entry with
  the onycholysis reformulation, one confounder challenge, a conceded item 5). UI
  builds entirely from this; it needs no backend.
- `fixtures/fixture_packet.json`, `fixtures/fixture_challenges.json` — one
  schema-valid packet and one challenge set, for ORCH (against stubs) and AGENTS
  (B input).
- `fixtures/corpus_tiny/` — 2 tiny notes (one with `PASI 14.2`, one nursing note
  with `onycholysis`) so TOOLS can unit-test without the full corpus.

Commit. Freeze.

---

## PHASE 1 — PARALLEL SUBAGENTS

### WS-CORPUS — authors `corpus/patient1/**` (+ leaves twin scaffolding note for Phase 2)
Author ~10 markdown notes, realistic headers (date, author, note type). Exact
values from §C below. **Acceptance (scriptable):**
- `grep -rl "adalimumab" corpus/patient1 | grep visit3` non-empty; not present in
  visit1/visit2 notes (fix #3).
- `grep -ri "PASI 6.1\|PASI 14.2\|PASI 16.8" corpus/patient1` all three hit.
- Burial: `grep -ri "nail involvement" corpus/patient1` → 0 hits; `grep -ri
  "onycholysis" corpus/patient1` → nursing note only (fix #2 depends on this).
- Ambiguity tripwire: `grep -rani "after I stopped\|because of the flare\|since the flare" corpus/patient1/*portal*` → 0 hits; portal message flare onset worded "past few weeks", no date earlier than 2026-06-07 tied to onset (fix #1).
- Dates monotone across visit filenames; no typo'd-date doc (fix #5).

### WS-TOOLS — implements `tools.py` against `fixtures/corpus_tiny/`
Keyword prefilter (token overlap, plain Python — no API) → ONE Claude call to
rank + extract verbatim quotes, with the corpus/system prefix cache-marked
(config.CACHE_PREFIX). No embeddings. **Acceptance:**
- `verify_quote` true on an exact substring, false on a near-miss (unit test).
- `chart_search("scalp plaque", corpus_tiny)` returns ranked list with verbatim
  quotes + doc_id + date.
- Demonstrate the reformulation miss: `chart_search("nail involvement", ...)`
  returns nothing from a derm note; `chart_search("onycholysis", ...)` hits the
  nursing note.

### WS-AGENTS — implements `agents.py` + `prompts/` against MOCKED tools
Use spec §4 prompt drafts with the invariant corrections. **assemble** runs A's
3-attempt-per-item loop, calls `verify_quote` before a quote enters the packet,
emits run_state incl. `search_trace`. **review** = B, no tools, packet+rubric
only, emits challenges of one of the two legal shapes (invariant #1). **repair**
targets `what_would_satisfy`; if the chart can't satisfy, sets item `insufficient`
and states what documentation would settle it (concession). All three modes send a cache-marked
stable prefix (system prompt + rubric [+ corpus for A]) with only the per-call
query/packet as the variable tail. **Acceptance:**
- `assemble` on a canned tool-response fixture emits a schema-valid run_state.
- `review(fixture_packet, rubric)` emits a schema-valid challenge whose
  `challenge_reason` references only packet/rubric content — assert it contains no
  doc_id or quote not present in the packet (guards invariant #1).
- `repair` produces a conceded item-5 given a confounder challenge.

### WS-ORCH — implements `orchestrator.py` against STUBBED agents
Stub assemble/review/repair to return canned fixtures. Implement the §5 loop with
fix #4. **Acceptance:**
- Stub: item 5 conceded + B stands down at round 2 → terminal
  `INSUFFICIENT_EVIDENCE` (NOT requiring round 3) — fix #4.
- Stub: all items airtight, B empty round 1 → `CONFIRMED_WORSENING`.
- 3-round cap enforced; a run_state JSON persisted to `runs/` every round.

### WS-UI — implements `app.py` + `static/index.html` against `fixture_runstate.json`
FastAPI serves `index.html` + `/state` (polls latest run_state). Static HTML,
NO Streamlit. Render: rubric×status matrix with chips; expandable evidence
(quote + doc_id + date); **search-trace panel** (the "where's the loop" answer);
round transcript with **packet diff** between rounds. **Acceptance:** loads
`fixture_runstate.json` and renders all four regions with zero backend/Claude
dependency. Frontend design: clinical, legible, restrained — the transcript is
the hero, not chrome.

---

## PHASE 2 — INTEGRATION (you, serial)

1. Wire real tools → agents → orchestrator; point `CORPUS_DIR` at `corpus/patient1`.
2. **Run the five integration gates:** (i) B challenge references no unseen chart
   fact; (ii) buried finding appears in search_trace, not a B round; (iii)
   adalimumab origin resolves item 4; (iv) conceded item → INSUFFICIENT_EVIDENCE;
   (v) no typo-date doc cited. Any fail → fix before proceeding.
3. **Build the twin:** copy `patient1/` → `patient1_twin/`, replace the adherence
   note with "adherence confirmed, no gaps, no interruptions." Expected terminal
   `CONFIRMED_WORSENING`. This buys the demo's success case.
4. Run patient1 (→ INSUFFICIENT_EVIDENCE), twin (→ CONFIRMED_WORSENING), and a
   V1–V2-only slice (→ STABLE). Assert against the ground-truth sheet.
5. **Cache the best patient1 run** to `runs/` for replay. This is the demo floor.
6. If clean and time remains: 3-row eval grid (patient1 → INSUFFICIENT; twin →
   CONFIRMED; patient1 + one injected fake quote → verify_quote rejects). Else
   append to `LATER.md`.

**Tripwire:** if `assemble`+`review` aren't producing a clean adversarial run,
ship Agent-A-only (assembler + search trace + verify_quote) as the deliverable —
a finished assembler beats a broken loop.

---

## §C — CORPUS SHEET (exact values for WS-CORPUS)

Patient 1, one patient across 4 visits. Every rubric number an exact quotable
string. Dose math: adalimumab q2wk from 5/10 → taken 5/10, 5/24; missed 6/7,
6/21; V4 on 6/29 → "two missed doses" exact.

| Visit | Date | Docs | PASI | BSA | IGA | DLQI | Therapy | Must contain |
|---|---|---|---|---|---|---|---|---|
| V1 | 2026-02-02 | derm note (meds inline) | 6.1 | 8% | 2 | 7 | clobetasol + NB-UVB 3×/wk | clean baseline, plainly stated |
| V2 | 2026-03-16 | derm note + duplicate med list | 6.3 | 8% | 2 | 8 | unchanged | "no new plaques, tolerating phototherapy"; flat scores, busy-looking meds (decoy) |
| V3 | 2026-05-10 | derm note, **nursing note**, labs (text, may sit in derm note) | 14.2 | 15% | 3 | 15 | **starts adalimumab 40 mg q2wk (first dose 5/10)**; NB-UVB continues | PASI jump in derm note only; **"onycholysis" + scalp plaques ONLY in nursing note**; never phrase "nail involvement"; CRP 9, ESR 24 |
| noise | 2026-05-26 | urgent-care URI note (optional, skip if slow) | — | — | — | — | — | placed after V3 labs so CRP stays ambiguous |
| V4 | 2026-06-29 | brief derm note, patient portal message, MA/pharmacy adherence note | 16.8 | 18% | 3 | 17 | taken 5/10 + 5/24, **missed 6/7 + 6/21**; 3-wk NB-UVB gap | flare report and adherence facts in **separate docs** |

**V4 ambiguity rules (one violation kills the confounder beat):** portal message
never dates onset relative to 6/7 ("worse over the past few weeks"); no causal
language either direction; miss reason mundane (pharmacy/shipping delay, travel),
never flare-motivated.

**Ground-truth sheet (WS-CORPUS also writes `corpus/GROUND_TRUTH.md`):**
patient1 full → item 1 worsening but confounded, terminal INSUFFICIENT_EVIDENCE;
twin → CONFIRMED_WORSENING; V1–V2 only → STABLE.