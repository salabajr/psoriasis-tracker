"""Results viewer + live demo server for the adversarial chart-review loop.

GET  /        -> static/index.html
GET  /state   -> {"latest": <run_state|null>, "rounds": [...]}  (unchanged)
GET  /events?after=N -> {"events": [...], "next": M, "running": bool}
GET  /presets -> {"patient1": <text>, "patient1_twin": <text>, "v1_v2_slice": <text>}
POST /run     -> {"notes_text": "...", "label": "..."} — one Run entry point.
                 If the pasted chart matches a preset with a recording in
                 runs/cached/, its event stream replays time-compressed to
                 ~1 minute; any other (new/edited) chart runs the models live.
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
CACHE_DIR = RUNS_DIR / "cached"      # recorded live runs used for fast replay
REPLAY_TARGET_SECONDS = 55.0         # cached demo plays end-to-end in ~1 min
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
        pid = None
        latest, _ = _collect_runs()
        if latest:
            candidate = RUNS_DIR / ("%s_events.jsonl" % latest.get("patient_id"))
            if candidate.exists():
                path, pid = candidate, latest.get("patient_id")
    evts = []
    run_id = None
    if path is not None:
        try:
            with path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            if lines:
                try:
                    # The stream's identity: its opening "start" event's
                    # timestamp. A same-patient restart truncates the file and
                    # gets a fresh timestamp, so the UI can detect it.
                    run_id = json.loads(lines[0]).get("t")
                except (json.JSONDecodeError, AttributeError):
                    run_id = None
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
                         "pid": pid, "run_id": run_id,
                         "running": _is_running(),
                         "error": _active["error"]})


def _preset(patient: str, through: str = None) -> dict:
    pdir = CORPUS_DIR / patient
    blocks = []
    for f in sorted(pdir.glob("*.md")):
        if through and f.stem.split("_")[0] > through:
            continue
        blocks.append("=== %s ===\n%s" % (f.name, f.read_text(encoding="utf-8").strip()))
    images = []
    for img in sorted((pdir / "images").glob("*.jpg")):
        visit = img.stem.split("_")[0]
        if through and visit > through:
            continue
        site = img.stem.split("_", 1)[1] if "_" in img.stem else img.stem
        images.append({
            "file": "images/%s" % img.name,
            "visit": visit,
            "label": "%s — %s" % (visit.replace("visit", "Visit "), site),
            "url": "/corpus/%s/images/%s" % (patient, img.name),
        })
    return {"text": "\n\n".join(blocks), "images": images}


@app.get("/presets")
def get_presets():
    return JSONResponse({
        "patient1": _preset("patient1"),
        "patient1_twin": _preset("patient1_twin"),
        "v1_v2_slice": _preset("patient1", through="visit2"),
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


def _run_live(notes, pid, images_from=""):
    corpus_root = Path(tempfile.mkdtemp(prefix="demo_corpus_"))
    pdir = corpus_root / pid
    pdir.mkdir()
    for fname, content in notes:
        (pdir / fname).write_text(content, encoding="utf-8")
    # Carry the source preset's photographs so Agent A's vision pass sees them.
    src_patient = {"patient1": "patient1", "patient1_twin": "patient1_twin",
                   "v1_v2_slice": "patient1"}.get(images_from)
    if src_patient:
        src = CORPUS_DIR / src_patient / "images"
        if src.is_dir():
            (pdir / "images").mkdir()
            note_visits = {fname.split("_")[0] for fname, _ in notes}
            for img in sorted(src.glob("*")):
                if img.suffix.lower() in (".jpg", ".jpeg", ".png") and \
                        img.stem.split("_")[0] in note_visits:
                    shutil.copy(img, pdir / "images" / img.name)
    try:
        from orchestrator import run
        run(str(pdir))
    except Exception as exc:  # surface to the UI instead of dying silently
        _active["error"] = "%s: %s" % (type(exc).__name__, exc)
        events_mod.emit("sys", "error", _active["error"])
        events_mod.stop_run()
    finally:
        shutil.rmtree(corpus_root, ignore_errors=True)


def _cached_source(notes_text: str):
    """Return the preset pid whose chart text exactly matches the pasted notes
    AND has a recording in runs/cached/ — else None (run live)."""
    key = (notes_text or "").strip()
    if not key:
        return None
    for src in ("patient1", "patient1_twin"):
        if not (CACHE_DIR / ("%s_events.jsonl" % src)).exists():
            continue
        try:
            if key == _preset(src)["text"].strip():
                return src
        except OSError:
            continue
    return None


def _run_replay_recorded(src_pid: str):
    """Replay a recorded live run's event stream, time-compressed to ~1 min.

    Inter-event gaps are scaled so the whole recording spans about
    REPLAY_TARGET_SECONDS — the typing deltas, tool calls, and verdict land
    with the same rhythm as the live run, just faster. Every event carries a
    "pct" so the UI progress bar tracks the playback exactly. Round/final
    state files are re-persisted at the same points the live run wrote them.
    """
    pid = "%s_replay" % src_pid
    try:
        with (CACHE_DIR / ("%s_events.jsonl" % src_pid)).open(
                "r", encoding="utf-8") as f:
            evts = [json.loads(l) for l in f if l.strip()]
        evts = [e for e in evts if e.get("type") != "start"]  # start_run re-emits
        t0, t_end = evts[0]["t"], evts[-1]["t"]
        span = max(t_end - t0, 1e-6)
        scale = min(1.0, REPLAY_TARGET_SECONDS / span)

        def persist(name: str) -> None:
            state = _load_json(CACHE_DIR / ("%s_%s.json" % (src_pid, name)))
            if state:
                state["patient_id"] = pid
                (RUNS_DIR / ("%s_%s.json" % (pid, name))).write_text(
                    json.dumps(state, indent=2), encoding="utf-8")

        events_mod.start_run(pid)
        prev = t0
        for e in evts:
            time.sleep(min(max((e["t"] - prev) * scale, 0.0), 4.0))
            prev = e["t"]
            data = dict(e.get("data") or {})
            # Progress synced to playback position (95 reserved for terminal).
            data["pct"] = round(2 + 93.0 * (e["t"] - t0) / span, 1)
            if e["type"] == "round":
                # The live loop persists round N-1 before announcing round N.
                persist("round%d" % (int(data.get("round", 1)) - 1))
            if e["type"] == "terminal":
                for p in CACHE_DIR.glob("%s_round*.json" % src_pid):
                    persist(p.stem[len(src_pid) + 1:])
                persist("final")
            events_mod.emit(e["actor"], e["type"], e.get("text", ""), **data)
    except Exception as exc:
        _active["error"] = "replay failed: %s" % exc
        events_mod.emit("sys", "error", _active["error"])
    finally:
        events_mod.stop_run()


class RunRequest(BaseModel):
    notes_text: str = ""
    label: str = ""
    images_from: str = ""


@app.post("/run")
def post_run(req: RunRequest):
    with _run_lock:
        if _is_running():
            return JSONResponse({"ok": False, "error": "a run is already in progress"},
                                status_code=409)
        _active["error"] = None
        notes = _parse_notes(req.notes_text or "")
        if not notes:
            return JSONResponse(
                {"ok": False,
                 "error": "no notes found — separate notes with lines like "
                          "=== visit1_derm_note.md ==="},
                status_code=400)
        cached = _cached_source(req.notes_text)
        if cached:
            pid = "%s_replay" % cached
            _clear_patient_artifacts(pid)
            (RUNS_DIR / ("%s_events.jsonl" % pid)).write_text("", encoding="utf-8")
            t = threading.Thread(target=_run_replay_recorded, args=(cached,),
                                 daemon=True)
        else:
            pid = re.sub(r"[^\w\-]", "_", req.label.strip()) or "pasted_chart"
            _clear_patient_artifacts(pid)
            (RUNS_DIR / ("%s_events.jsonl" % pid)).write_text("", encoding="utf-8")
            t = threading.Thread(target=_run_live,
                                 args=(notes, pid, req.images_from), daemon=True)
        _active["thread"] = t
        _active["patient_id"] = pid
        t.start()
    return JSONResponse({"ok": True, "patient_id": pid,
                         "mode": "cached" if cached else "live"})


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/corpus", StaticFiles(directory=CORPUS_DIR), name="corpus")
