"""Append-only run event stream for the live demo UI.

orchestrator.run() calls start_run(patient_id); agents/orchestrator then call
emit(). When no run has been started (e.g. unit tests calling agents
directly), emit() is a silent no-op — the verified pipeline is unchanged.

Events land in runs/<patient_id>_events.jsonl, one JSON object per line:
  {"t": <epoch>, "actor": "A"|"B"|"sys", "type": "...", "text": "...", "data": {...}}
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
