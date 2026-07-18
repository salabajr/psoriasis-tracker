"""Append-only run event stream for the live demo UI.

orchestrator.run() calls start_run(patient_id); agents/orchestrator then call
emit(). When no run has been started (e.g. unit tests calling agents
directly), emit() is a silent no-op — the verified pipeline is unchanged.

Events land in runs/<patient_id>_events.jsonl, one JSON object per line:
  {"t": <epoch>, "actor": "A"|"B"|"sys", "type": "...", "text": "...", "data": {...}}

TRANSCRIPT STYLE RULES — every emit()'s text is read aloud in a live demo by
clinicians, not engineers:
  1. One or two short sentences, first person ("We found…", "We're checking…"),
     as the agent speaking in its clinical role (chart reviewer / nurse).
  2. No engineering jargon: never "packet schema", "contract guard", "JSON",
     "criterion_id". Say "question 5", "the chart", "the rubric".
  3. One event per meaningful step — never one per search query or per photo.
     Machine detail belongs in the **data kwargs and the side panels, not in
     the text.
  4. Model-written fields that reach the transcript (challenge reasons, gaps,
     photo captions) follow the same rules — enforced in prompts/*.txt.
"""
import json
import os
import threading
import time

_lock = threading.Lock()
_path = None

RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")


def start_run(patient_id: str) -> None:
    global _path
    os.makedirs(RUNS_DIR, exist_ok=True)
    with _lock:
        _path = os.path.join(RUNS_DIR, "%s_events.jsonl" % patient_id)
        with open(_path, "w"):
            pass  # truncate any previous stream for this patient
    emit("sys", "start", "Run started", patient_id=patient_id)


def is_active() -> bool:
    """True while a run's event stream is open (demo UI is watching)."""
    return _path is not None


def stop_run() -> None:
    global _path
    with _lock:
        _path = None


def emit(actor: str, etype: str, text: str, **data) -> None:
    if _path is None:
        return
    evt = {"t": round(time.time(), 3), "actor": actor, "type": etype, "text": text}
    if data:
        evt["data"] = data
    try:
        with _lock, open(_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt) + "\n")
    except OSError:
        pass  # the demo stream must never take down a run
