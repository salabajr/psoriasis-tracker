"""Results viewer for the adversarial chart-review loop.

GET /       -> static/index.html
GET /state  -> {"latest": <run_state|null>, "rounds": [<run_state>, ...]}
               from runs/<pid>_round*.json (most recently modified patient),
               falling back to fixtures/fixture_runstate.json when runs/ is empty.
"""

import json
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "runs"
STATIC_DIR = BASE_DIR / "static"
FIXTURE_PATH = BASE_DIR / "fixtures" / "fixture_runstate.json"

ROUND_FILE_RE = re.compile(r"^(?P<pid>.+)_round(?P<round>\d+)\.json$")

app = FastAPI(title="Chart Review Viewer")


def _load_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _collect_runs():
    """Return (latest, rounds) for the most recently modified patient in runs/,
    or (None, []) if there are no usable round files."""
    if not RUNS_DIR.is_dir():
        return None, []

    by_patient = {}  # pid -> list of (round:int, mtime:float, path)
    for path in RUNS_DIR.iterdir():
        if not path.is_file():
            continue
        m = ROUND_FILE_RE.match(path.name)
        if not m:
            continue  # ignore .gitkeep, _final.json, non-JSON, etc.
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        by_patient.setdefault(m.group("pid"), []).append(
            (int(m.group("round")), mtime, path)
        )

    if not by_patient:
        return None, []

    # Most recently modified patient = patient owning the newest round file.
    pid = max(by_patient, key=lambda p: max(e[1] for e in by_patient[p]))

    rounds = []
    for rnd, _mtime, path in sorted(by_patient[pid], key=lambda e: e[0]):
        state = _load_json(path)
        if isinstance(state, dict):
            rounds.append(state)

    if not rounds:
        return None, []
    return rounds[-1], rounds


@app.get("/state")
def get_state():
    latest, rounds = _collect_runs()
    if latest is None:
        fixture = _load_json(FIXTURE_PATH)
        if fixture is None:
            return JSONResponse({"latest": None, "rounds": []})
        return JSONResponse({"latest": fixture, "rounds": [fixture]})
    return JSONResponse({"latest": latest, "rounds": rounds})


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
