# contracts.md — FROZEN after Phase 0 commit. Do not edit.

If you believe a contract here is wrong: STOP and report to the lead. Do not
edit this file — edits silently break sibling workstreams.

## Frozen function signatures

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

## Frozen JSON schemas

```jsonc
// packet entry
{"criterion_id":1,"criterion_text":"...","status":"worsening|stable|improving|insufficient",
 "evidence":[{"doc_id":"...","quote":"...","date":"YYYY-MM-DD"}],
 "reasoning":"...","rubric_ref":"..."}

// challenge (B -> A)
{"criterion_id":1,"challenge_reason":"...","rubric_quote":"...","what_would_satisfy":"..."}

// run_state (persisted every round -> runs/, drives UI)
{"patient_id":"...","round":0,"terminal_state":null,
 "packet":[/*entries*/],"challenges":[/*per round*/],
 "search_trace":[{"criterion_id":2,"attempts":["nail involvement","onycholysis"],
                  "resolved_in":"visit3_nursing_note"}]}
```

`terminal_state ∈ {CONFIRMED_WORSENING, STABLE, INSUFFICIENT_EVIDENCE, null}`.

## Binding clarifications (part of the freeze)

1. **doc_id** = the markdown filename stem: no directory, no `.md` extension.
   Example: `corpus/patient1/visit3_nursing_note.md` → `"visit3_nursing_note"`.
2. **`run_state.challenges` is a list of per-round lists.**
   `challenges[i]` is the array of challenge objects B issued in round `i+1`.
   An empty array means B stood down that round.
   Example: `[[{...confounder challenge...}], []]` = one challenge in round 1,
   stand-down in round 2.
3. **Dates** are always `YYYY-MM-DD` strings.
4. **Evidence quotes** must be verbatim substrings of the named doc;
   `verify_quote(doc_id, quote, corpus_dir)` must return True for every
   evidence entry before it enters a packet.
5. **`rubric_ref`** is the string form of the rubric item id (e.g. `"5"`),
   optionally with a short pointer into its text.
6. **run_state persistence**: orchestrator writes
   `runs/<patient_id>_round<round>.json` after every round, and
   `runs/<patient_id>_final.json` at terminal.
7. **search_trace** entries are appended only by ASSEMBLE (and repair, if it
   searches). `attempts` is the ordered list of query strings tried;
   `resolved_in` is the doc_id that finally satisfied the query, or `null`.
8. **Agent B (review) has no chart access.** Its inputs are packet + rubric
   ONLY. A challenge that references a doc_id or quote absent from the packet
   is a contract violation.
9. **Terminal logic**: if any load-bearing item has status `insufficient`
   (conceded) at the moment B stands down, terminal = `INSUFFICIENT_EVIDENCE`,
   regardless of round count. All items airtight + B stands down =
   `CONFIRMED_WORSENING` (or `STABLE` if item 1 is stable). The 3-round cap is
   an upper bound, never a requirement.
10. **Model/config**: every Claude call imports `MODEL` / `TEMPERATURE` from
    `config.py`. No inline model strings. When `CACHE_PREFIX` is True the
    stable prefix (system prompt + rubric [+ corpus for A]) carries
    `cache_control: {"type": "ephemeral"}` and the variable query/packet is the
    tail — never interleaved into the cached block.
