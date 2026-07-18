"""Adversarial loop orchestrator.

Drives the assemble -> review -> repair loop per contracts.md section 5:
  round 0: Agent A assembles the packet from the chart.
  rounds 1..MAX_ROUNDS: Agent B reviews (packet + rubric only); if it issues
  challenges, Agent A repairs; if it stands down (empty challenge list) the
  loop terminates immediately.

Terminal state is computed WHENEVER B stands down or the round cap is hit —
the 3-round cap is an upper bound, never a requirement (contracts.md #9).

run_state is persisted to runs/<patient_id>_round<n>.json after every round
(including round 0) and runs/<patient_id>_final.json at terminal.
"""

import json
import os

from config import MAX_ROUNDS

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(_REPO_ROOT, "runs")
RUBRIC_PATH = os.path.join(_REPO_ROOT, "rubric.json")

# Rubric items whose failure alone forces INSUFFICIENT_EVIDENCE.
LOAD_BEARING_IDS = {1, 5}


def _load_rubric() -> list:
    with open(RUBRIC_PATH, "r") as f:
        return json.load(f)


def _compute_terminal(packet: list) -> str:
    """Terminal logic (contracts.md binding clarification #9).

    - Any load-bearing item (ids 1, 5) with status "insufficient"
      -> INSUFFICIENT_EVIDENCE, regardless of round count.
    - Else item 1 "worsening" -> CONFIRMED_WORSENING.
    - Else -> STABLE.
    """
    status_by_id = {entry.get("criterion_id"): entry.get("status") for entry in packet}
    if any(status_by_id.get(cid) == "insufficient" for cid in LOAD_BEARING_IDS):
        return "INSUFFICIENT_EVIDENCE"
    if status_by_id.get(1) == "worsening":
        return "CONFIRMED_WORSENING"
    return "STABLE"


def _persist(run_state: dict, filename: str) -> None:
    os.makedirs(RUNS_DIR, exist_ok=True)
    path = os.path.join(RUNS_DIR, filename)
    with open(path, "w") as f:
        json.dump(run_state, f, indent=2)


def _persist_round(run_state: dict) -> None:
    _persist(run_state, "%s_round%d.json" % (run_state["patient_id"], run_state["round"]))


def _persist_final(run_state: dict) -> None:
    _persist(run_state, "%s_final.json" % run_state["patient_id"])


def run(patient_dir: str, assemble=None, review=None, repair=None) -> dict:
    """Run the adversarial loop for one patient. Returns the final run_state.

    assemble/review/repair are injectable for testing; by default they resolve
    lazily from agents.py so importing this module never imports agents.
    """
    if assemble is None or review is None or repair is None:
        import agents  # deferred: tests inject stubs and never touch agents.py

        assemble = assemble or agents.assemble
        review = review or agents.review
        repair = repair or agents.repair

    rubric = _load_rubric()
    patient_id = os.path.basename(os.path.normpath(patient_dir))

    # Round 0: assemble.
    run_state = assemble(rubric, patient_dir)
    run_state["patient_id"] = patient_id
    run_state["round"] = 0
    run_state["terminal_state"] = None
    run_state.setdefault("packet", [])
    run_state.setdefault("challenges", [])
    run_state.setdefault("search_trace", [])
    _persist_round(run_state)

    # Rounds 1..MAX_ROUNDS: review / repair.
    for round_num in range(1, MAX_ROUNDS + 1):
        challenges = review(run_state["packet"], rubric)
        run_state["challenges"].append(challenges)
        run_state["round"] = round_num

        if not challenges:
            # B stands down: terminal NOW, regardless of round count.
            run_state["terminal_state"] = _compute_terminal(run_state["packet"])
            _persist_round(run_state)
            _persist_final(run_state)
            return run_state

        run_state["packet"] = repair(run_state["packet"], challenges, patient_dir)
        _persist_round(run_state)

    # Round cap reached with B still challenging: terminal on current packet.
    run_state["terminal_state"] = _compute_terminal(run_state["packet"])
    _persist_final(run_state)
    return run_state


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python orchestrator.py <patient_dir>")
        sys.exit(1)
    final_state = run(sys.argv[1])
    print(json.dumps(final_state, indent=2))
