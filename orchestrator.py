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
import events


def _load_rubric() -> list:
    with open(RUBRIC_PATH, "r") as f:
        return json.load(f)


def _compute_terminal(packet: list) -> str:
    """Terminal logic (contracts.md binding clarification #9).

    - Item 1 "insufficient" -> INSUFFICIENT_EVIDENCE: no severity claim can
      be made at all.
    - Item 1 "worsening" with item 5 "insufficient" -> INSUFFICIENT_EVIDENCE:
      the worsening claim stands on an unaddressed confounder. Item 5 is
      load-bearing only when a worsening attribution is on the table — the
      rubric's confounder rule is conditional ("Worsening cannot be
      attributed ... if a confounder ... is unaddressed"), so with item 1
      stable there is no attribution for a confounder to defeat.
    - Else item 1 "worsening" -> CONFIRMED_WORSENING.
    - Else -> STABLE.
    Terminal is computed whenever B stands down or the round cap is hit,
    regardless of round count.
    """
    status_by_id = {entry.get("criterion_id"): entry.get("status") for entry in packet}
    if status_by_id.get(1) == "insufficient":
        return "INSUFFICIENT_EVIDENCE"
    if status_by_id.get(1) == "worsening":
        if status_by_id.get(5) == "insufficient":
            return "INSUFFICIENT_EVIDENCE"
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


def _consume_tool_trace() -> list:
    """Drain Agent B's tool-call trace (empty when agents is stubbed/unused).

    Never imports agents — stubbed test runs must not load it.
    """
    import sys as _sys
    mod = _sys.modules.get("agents")
    if mod is None:
        return []
    try:
        return mod.consume_tool_trace()
    except Exception:
        return []


def _maybe_notify(run_state: dict, notify) -> None:
    """Agent B as communicator: patient-facing SMS on a decisive verdict."""
    if notify is None:
        return
    try:
        message = notify(run_state)
    except Exception:
        return
    if message:
        run_state["patient_message"] = message
        events.emit("B", "sms", message, terminal=run_state["terminal_state"])


def run(patient_dir: str, assemble=None, review=None, repair=None, notify=None) -> dict:
    """Run the adversarial loop for one patient. Returns the final run_state.

    assemble/review/repair/notify are injectable for testing; by default they
    resolve lazily from agents.py so importing this module never imports agents.
    """
    if assemble is None or review is None or repair is None:
        import agents  # deferred: tests inject stubs and never touch agents.py

        assemble = assemble or agents.assemble
        review = review or agents.review
        repair = repair or agents.repair
        notify = notify or getattr(agents, "notify", None)

    rubric = _load_rubric()
    patient_id = os.path.basename(os.path.normpath(patient_dir))
    events.start_run(patient_id)
    try:
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
        challenge_rounds_by_cid = {}
        for round_num in range(1, MAX_ROUNDS + 1):
            events.emit("sys", "round", "Round %d: adversarial review" % round_num,
                        round=round_num)
            challenges = review(run_state["packet"], rubric)
            for trace_entry in _consume_tool_trace():
                trace_entry["round"] = round_num
                run_state.setdefault("tool_trace", []).append(trace_entry)
            run_state["challenges"].append(challenges)
            run_state["round"] = round_num

            if not challenges:
                # B stands down: terminal NOW, regardless of round count.
                run_state["terminal_state"] = _compute_terminal(run_state["packet"])
                events.emit("sys", "terminal", run_state["terminal_state"],
                            terminal=run_state["terminal_state"], round=round_num)
                _maybe_notify(run_state, notify)
                _persist_round(run_state)
                _persist_final(run_state)
                return run_state

            run_state["packet"] = repair(run_state["packet"], challenges, patient_dir)

            # Two-strikes rule: if B challenges the same criterion in a second
            # round, A's repair has already had its one full chance and failed
            # to convince — force the concession rather than looping to the
            # cap on an unwinnable point.
            for challenge in challenges:
                cid = challenge.get("criterion_id")
                challenge_rounds_by_cid[cid] = challenge_rounds_by_cid.get(cid, 0) + 1
                if challenge_rounds_by_cid[cid] >= 2:
                    for entry in run_state["packet"]:
                        if entry.get("criterion_id") == cid and \
                                entry.get("status") != "insufficient":
                            entry["status"] = "insufficient"
                            entry["reasoning"] = (
                                "Conceded after repeated challenge: two repair "
                                "attempts could not satisfy Agent B. "
                                + str(challenge.get("what_would_satisfy", ""))
                            )
                            events.emit(
                                "sys", "info",
                                "Question %s has now been disputed twice "
                                "without resolution — it's marked 'not enough "
                                "documentation' and the debate moves on."
                                % cid, criterion=cid)

            # If A conceded every point B raised (nothing challenged kept a
            # non-insufficient status), another review round could only
            # re-confirm the concessions — close the debate now instead of
            # spending a full model round on a foregone stand-down.
            challenged = {c.get("criterion_id") for c in challenges
                          if isinstance(c, dict)}
            by_id = {e.get("criterion_id"): e for e in run_state["packet"]}
            if challenged and all(
                    (by_id.get(cid) or {}).get("status") == "insufficient"
                    for cid in challenged):
                events.emit("sys", "info",
                            "Agent A has conceded every point Agent B raised — "
                            "nothing is left in dispute, so the review closes "
                            "here.")
                run_state["terminal_state"] = _compute_terminal(run_state["packet"])
                events.emit("sys", "terminal", run_state["terminal_state"],
                            terminal=run_state["terminal_state"], round=round_num)
                _maybe_notify(run_state, notify)
                _persist_round(run_state)
                _persist_final(run_state)
                return run_state
            _persist_round(run_state)

        # Round cap reached with B still challenging: terminal on current packet.
        run_state["terminal_state"] = _compute_terminal(run_state["packet"])
        events.emit("sys", "terminal", run_state["terminal_state"],
                    terminal=run_state["terminal_state"], round=run_state["round"])
        _maybe_notify(run_state, notify)
        _persist_final(run_state)
        return run_state
    finally:
        events.stop_run()


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python orchestrator.py <patient_dir>")
        sys.exit(1)
    final_state = run(sys.argv[1])
    print(json.dumps(final_state, indent=2))
