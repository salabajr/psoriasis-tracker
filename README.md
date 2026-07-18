# psoriasis-tracker

Two-agent adversarial disease-progression monitor for plaque psoriasis.

**Agent A (assembler)** searches a synthetic patient chart, builds an
evidence packet against a 5-item rubric (every quote verbatim-verified).
**Agent B (reviewer)** sees only the packet + rubric — no chart — and
challenges unsupported attributions. The orchestrator runs up to 3
challenge/repair rounds and emits a terminal state:
`CONFIRMED_WORSENING | STABLE | INSUFFICIENT_EVIDENCE`.

## Layout

- `contracts.md` — frozen function signatures + JSON schemas (read first)
- `config.py` — model/temperature/rounds/corpus config (only place a model string lives)
- `rubric.json` — the 5 progression criteria
- `tools.py` — chart_search / rubric_lookup / verify_quote
- `agents.py`, `prompts/` — assemble / review / repair
- `orchestrator.py` — the adversarial loop; persists run_state to `runs/`
- `app.py`, `static/` — FastAPI + static HTML viewer (polls latest run_state)
- `corpus/patient1/` — synthetic chart (4 visits); `corpus/patient1_twin/` — adherence-clean twin
- `fixtures/` — hand-authored packet/challenges/run_state + tiny corpus for isolated testing
- `main.py` — entry point

## Run

```bash
uv venv && uv pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
python main.py --patient corpus/patient1        # full adversarial run
uvicorn app:app --port 8000                     # UI at http://localhost:8000
```

Expected terminals: patient1 → INSUFFICIENT_EVIDENCE (unaddressed
non-adherence confounder); patient1_twin → CONFIRMED_WORSENING;
patient1 V1–V2 slice → STABLE. See `corpus/GROUND_TRUTH.md`.
