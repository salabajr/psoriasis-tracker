"""Results viewer + live demo server for the adversarial chart-review loop.

GET  /        -> static/index.html
GET  /state   -> {"latest": <run_state|null>, "rounds": [...]}  (unchanged)
GET  /events?after=N -> {"events": [...], "next": M, "running": bool}
GET  /presets -> {"patient1": <text>, "patient1_twin": <text>, "v1_v2_slice": <text>}
POST /run     -> {"notes_text": "...", "label": "..."} start a live run
                 {"replay": true} replay the committed patient1 demo run
"""

import json
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import events as events_mod

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "runs"
STATIC_DIR = BASE_DIR / "static"
CORPUS_DIR = BASE_DIR / "corpus"
FIXTURE_PATH = BASE_DIR / "fixtures" / "fixture_runstate.json"

ROUND_FILE_RE = re.compile(r"^(?P<pid>.+)_round(?P<round>\d+)\.json$")
NOTE_HEADER_RE = re.compile(r"^===\s*(?P<name>[\w.\- ]+?)\s*===\s*$")

app = FastAPI(title="Chart Review Viewer")

_run_lock = threading.Lock()
_active = {"thread": None, "patient_id": None, "error": None}


def _load_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _collect_runs():
    """Return (latest, rounds) for the active or most recently modified patient."""
    if not RUNS_DIR.is_dir():
        return None, []

    by_patient = {}
    for path in RUNS_DIR.iterdir():
        if not path.is_file():
            continue
        m = ROUND_FILE_RE.match(path.name)
        if not m:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        by_patient.setdefault(m.group("pid"), []).append(
            (int(m.group("round")), mtime, path)
        )

    if not by_patient:
        return None, []

    active_pid = _active["patient_id"]
    if active_pid in by_patient:
        pid = active_pid
    else:
        pid = max(by_patient, key=lambda p: max(e[1] for e in by_patient[p]))

    rounds = []
    for rnd, _mtime, path in sorted(by_patient[pid], key=lambda e: e[0]):
        state = _load_json(path)
        if isinstance(state, dict):
            rounds.append(state)

    if not rounds:
        return None, []
    return rounds[-1], rounds


def _is_running() -> bool:
    t = _active["thread"]
    return bool(t and t.is_alive())


@app.get("/state")
def get_state():
    latest, rounds = _collect_runs()
    if latest is None:
        fixture = _load_json(FIXTURE_PATH)
        if fixture is None:
            return JSONResponse({"latest": None, "rounds": [], "running": False})
        return JSONResponse({"latest": fixture, "rounds": [fixture],
                             "running": _is_running()})
    return JSONResponse({"latest": latest, "rounds": rounds,
                         "running": _is_running()})


@app.get("/events")
def get_events(after: int = 0):
    """Incremental event feed for the active (or most recent) run."""
    pid = _active["patient_id"]
    path = RUNS_DIR / ("%s_events.jsonl" % pid) if pid else None
    if path is None or not path.exists():
        # Fall back to the patient whose round files are newest (matches /state).
        latest, _ = _collect_runs()
        if latest:
            candidate = RUNS_DIR / ("%s_events.jsonl" % latest.get("patient_id"))
            path = candidate if candidate.exists() else None
    evts = []
    if path is not None:
        try:
            with path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[after:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    evts.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            after = len(lines)
        except OSError:
            pass
    return JSONResponse({"events": evts, "next": after,
                         "running": _is_running(),
                         "error": _active["error"]})


def _preset_text(patient: str, through: str = None) -> str:
    pdir = CORPUS_DIR / patient
    blocks = []
    for f in sorted(pdir.glob("*.md")):
        if through and f.stem.split("_")[0] > through:
            continue
        blocks.append("=== %s ===\n%s" % (f.name, f.read_text(encoding="utf-8").strip()))
    return "\n\n".join(blocks)


@app.get("/presets")
def get_presets():
    return JSONResponse({
        "patient1": _preset_text("patient1"),
        "patient1_twin": _preset_text("patient1_twin"),
        "v1_v2_slice": _preset_text("patient1", through="visit2"),
    })


def _parse_notes(text: str):
    """Split pasted text into (filename, content) on '=== name ===' headers."""
    notes, name, buf = [], None, []
    for line in text.splitlines():
        m = NOTE_HEADER_RE.match(line)
        if m:
            if name and "".join(buf).strip():
                notes.append((name, "\n".join(buf).strip() + "\n"))
            name = m.group("name").strip()
            if not name.endswith(".md"):
                name += ".md"
            buf = []
        else:
            buf.append(line)
    if name and "".join(buf).strip():
        notes.append((name, "\n".join(buf).strip() + "\n"))
    return notes


def _clear_patient_artifacts(pid: str) -> None:
    for p in RUNS_DIR.glob("%s_*.json" % pid):
        if ROUND_FILE_RE.match(p.name) or p.name == "%s_final.json" % pid:
            p.unlink(missing_ok=True)


def _run_live(notes, pid):
    corpus_root = Path(tempfile.mkdtemp(prefix="demo_corpus_"))
    pdir = corpus_root / pid
    pdir.mkdir()
    for fname, content in notes:
        (pdir / fname).write_text(content, encoding="utf-8")
    try:
        from orchestrator import run
        run(str(pdir))
    except Exception as exc:  # surface to the UI instead of dying silently
        _active["error"] = "%s: %s" % (type(exc).__name__, exc)
        events_mod.emit("sys", "error", _active["error"])
        events_mod.stop_run()
    finally:
        shutil.rmtree(corpus_root, ignore_errors=True)


def _run_replay():
    """Replay the committed patient1 demo run with theatrical pacing."""
    pid = "patient1_replay"
    src = [RUNS_DIR / ("patient1_round%d.json" % r) for r in range(0, 3)]
    final = RUNS_DIR / "patient1_final.json"
    try:
        events_mod.start_run(pid)
        snapshots = [s for s in (_load_json(p) for p in src) if s]
        events_mod.emit("A", "phase",
                        "Agent A is reading the chart and gathering evidence "
                        "for 5 rubric criteria")
        r0 = snapshots[0]
        for trace in r0.get("search_trace", []):
            for i, q in enumerate(trace["attempts"]):
                hit = trace["resolved_in"] if i == len(trace["attempts"]) - 1 or i > 0 else None
                resolved = trace["resolved_in"]
                miss = len(trace["attempts"]) > 1 and i == 0
                events_mod.emit(
                    "A", "search",
                    'Criterion %s: searched "%s" — %s'
                    % (trace["criterion_id"], q,
                       "no hits, reformulating" if miss
                       else "verified quotes from %s" % resolved),
                    criterion=trace["criterion_id"], query=q, hits=0 if miss else 1,
                )
                time.sleep(1.2)
            events_mod.emit("A", "tick",
                            "Criterion %s: evidence gathered" % trace["criterion_id"],
                            criterion=trace["criterion_id"],
                            resolved_in=trace["resolved_in"])
        events_mod.emit("A", "info",
                        "Agent A is drafting the evidence packet from the "
                        "verified quotes…")
        time.sleep(2)
        events_mod.emit("A", "packet", "Evidence packet assembled",
                        statuses={str(e["criterion_id"]): e["status"]
                                  for e in r0["packet"]})
        for state, path in zip(snapshots, src):
            out = dict(state, patient_id=pid)
            (RUNS_DIR / ("%s_round%d.json" % (pid, out["round"]))).write_text(
                json.dumps(out, indent=2), encoding="utf-8")
            time.sleep(0.5)
        fin = _load_json(final)
        for rnd_idx, rnd in enumerate(fin.get("challenges", []), 1):
            events_mod.emit("sys", "round",
                            "Round %d: adversarial review" % rnd_idx, round=rnd_idx)
            events_mod.emit("B", "phase",
                            "Agent B is reviewing the packet — packet + rubric "
                            "only, no chart access")
            time.sleep(2.5)
            if not rnd:
                events_mod.emit("B", "standdown",
                                "Agent B stands down — the packet is airtight "
                                "under the rubric")
            for c in rnd:
                def _trunc(s, n):
                    s = str(s or "")
                    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + " …"
                events_mod.emit(
                    "B", "challenge",
                    "Agent B challenges item %s: %s"
                    % (c["criterion_id"], _trunc(c["challenge_reason"], 320)),
                    criterion=c["criterion_id"],
                    rubric_quote=_trunc(c.get("rubric_quote", ""), 300),
                    what_would_satisfy=_trunc(c.get("what_would_satisfy", ""), 400))
                time.sleep(2)
                events_mod.emit("A", "phase",
                                "Agent A is repairing the packet — targeting "
                                "what would satisfy 1 challenge(s)")
                time.sleep(2)
                events_mod.emit("A", "concede",
                                "Agent A concedes item %s — the chart cannot "
                                "answer the challenge; status set to insufficient"
                                % c["criterion_id"], criterion=c["criterion_id"])
        out = dict(fin, patient_id=pid)
        (RUNS_DIR / ("%s_final.json" % pid)).write_text(
            json.dumps(out, indent=2), encoding="utf-8")
        events_mod.emit("sys", "terminal", fin["terminal_state"],
                        terminal=fin["terminal_state"], round=fin["round"])
    except Exception as exc:
        _active["error"] = "replay failed: %s" % exc
        events_mod.emit("sys", "error", _active["error"])
    finally:
        events_mod.stop_run()


class RunRequest(BaseModel):
    notes_text: str = ""
    label: str = ""
    replay: bool = False


@app.post("/run")
def post_run(req: RunRequest):
    with _run_lock:
        if _is_running():
            return JSONResponse({"ok": False, "error": "a run is already in progress"},
                                status_code=409)
        _active["error"] = None
        if req.replay:
            pid = "patient1_replay"
            if not (RUNS_DIR / "patient1_final.json").exists():
                return JSONResponse({"ok": False,
                                     "error": "no cached patient1 run to replay"},
                                    status_code=400)
            _clear_patient_artifacts(pid)
            (RUNS_DIR / ("%s_events.jsonl" % pid)).write_text("", encoding="utf-8")
            t = threading.Thread(target=_run_replay, daemon=True)
        else:
            notes = _parse_notes(req.notes_text or "")
            if not notes:
                return JSONResponse(
                    {"ok": False,
                     "error": "no notes found — separate notes with lines like "
                              "=== visit1_derm_note.md ==="},
                    status_code=400)
            pid = re.sub(r"[^\w\-]", "_", req.label.strip()) or "pasted_chart"
            _clear_patient_artifacts(pid)
            (RUNS_DIR / ("%s_events.jsonl" % pid)).write_text("", encoding="utf-8")
            t = threading.Thread(target=_run_live, args=(notes, pid), daemon=True)
        _active["thread"] = t
        _active["patient_id"] = pid
        t.start()
    return JSONResponse({"ok": True, "patient_id": pid})


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
