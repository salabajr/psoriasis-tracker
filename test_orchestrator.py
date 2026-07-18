"""Tests for orchestrator.run — all agent functions stubbed, no agents.py import.

Run: .venv/bin/python test_orchestrator.py
"""

import copy
import json
import os
import shutil
import sys
import tempfile

assert "agents" not in sys.modules, "tests must not import agents.py"

import orchestrator
from orchestrator import run, RUNS_DIR

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(REPO_ROOT, "fixtures")

RUN_STATE_KEYS = {"patient_id", "round", "terminal_state", "packet", "challenges", "search_trace"}

_created_files = []


def _load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r") as f:
        return json.load(f)


def _track_runs(patient_id):
    """Register every runs/ file this patient_id could produce, for teardown."""
    for n in range(0, 10):
        _created_files.append(os.path.join(RUNS_DIR, "%s_round%d.json" % (patient_id, n)))
    _created_files.append(os.path.join(RUNS_DIR, "%s_final.json" % patient_id))


def _make_run_state(packet, patient_id="zz_testpatient"):
    return {
        "patient_id": patient_id,
        "round": 0,
        "terminal_state": None,
        "packet": copy.deepcopy(packet),
        "challenges": [],
        "search_trace": [
            {"criterion_id": 2, "attempts": ["nail involvement", "onycholysis"],
             "resolved_in": "visit3_nursing_note"}
        ],
    }


def _make_patient_dir(tmp_root, name):
    d = os.path.join(tmp_root, name)
    os.makedirs(d, exist_ok=True)
    return d


def scenario_a(tmp_root):
    """Challenge in round 1, repair concedes item 5 -> the concede-all
    early-exit closes the debate at round 1 (no foregone round-2 stand-down;
    B is only called once)."""
    packet = _load_fixture("fixture_packet.json")
    challenges_fixture = _load_fixture("fixture_challenges.json")
    patient_dir = _make_patient_dir(tmp_root, "zz_testpatient")
    _track_runs("zz_testpatient")

    def assemble(rubric, corpus_dir):
        assert isinstance(rubric, list) and rubric[0]["id"] == 1
        return _make_run_state(packet)

    review_calls = {"n": 0}

    def review(pkt, rubric):
        review_calls["n"] += 1
        if review_calls["n"] == 1:
            return copy.deepcopy(challenges_fixture)
        return []

    def repair(pkt, challenges, corpus_dir):
        patched = copy.deepcopy(pkt)
        for entry in patched:
            if entry["criterion_id"] == 5:
                entry["status"] = "insufficient"
        return patched

    state = run(patient_dir, assemble=assemble, review=review, repair=repair)

    assert state["terminal_state"] == "INSUFFICIENT_EVIDENCE", state["terminal_state"]
    assert state["round"] == 1, ("concede-all early-exit: terminal at round 1, "
                                 "got %r" % state["round"])
    assert state["challenges"] == [challenges_fixture], state["challenges"]
    assert review_calls["n"] == 1, "B must not be called for a foregone stand-down"
    print("PASS scenario A: INSUFFICIENT_EVIDENCE at round 1 via concede-all "
          "early-exit, challenges == [[challenge]]")
    return state


def scenario_b(tmp_root):
    """All items airtight, B stands down immediately -> CONFIRMED_WORSENING at round 1."""
    packet = _load_fixture("fixture_packet.json")
    packet = copy.deepcopy(packet)
    for entry in packet:
        if entry["criterion_id"] == 1:
            entry["status"] = "worsening"
        elif entry["criterion_id"] == 5:
            entry["status"] = "stable"  # confounder addressed
        elif entry["status"] == "insufficient":
            entry["status"] = "stable"
    patient_dir = _make_patient_dir(tmp_root, "patientB")
    _track_runs("patientB")

    def assemble(rubric, corpus_dir):
        return _make_run_state(packet, patient_id="patientB")

    def review(pkt, rubric):
        return []

    def repair(pkt, challenges, corpus_dir):
        raise AssertionError("repair must not be called when B stands down in round 1")

    state = run(patient_dir, assemble=assemble, review=review, repair=repair)

    assert state["terminal_state"] == "CONFIRMED_WORSENING", state["terminal_state"]
    assert state["round"] == 1, state["round"]
    assert state["challenges"] == [[]], state["challenges"]
    print("PASS scenario B: CONFIRMED_WORSENING at round 1")


def scenario_c(tmp_root):
    """B challenges a DIFFERENT criterion each round (so neither the
    two-strikes rule nor the concede-all early-exit fires), repair never
    concedes -> loop stops at the round-3 cap.

    Rotates items 1, 2, 3 — all "worsening" in the fixture. (Item 4 is already
    "insufficient", so challenging it would trip the concede-all early-exit
    and never reach the genuine cap fall-through.)"""
    packet = _load_fixture("fixture_packet.json")
    challenge = _load_fixture("fixture_challenges.json")
    patient_dir = _make_patient_dir(tmp_root, "patientC")
    _track_runs("patientC")

    review_calls = {"n": 0}

    def assemble(rubric, corpus_dir):
        return _make_run_state(packet, patient_id="patientC")

    def review(pkt, rubric):
        review_calls["n"] += 1
        ch = copy.deepcopy(challenge)
        ch[0]["criterion_id"] = review_calls["n"]  # rounds hit items 1, 2, 3
        return ch

    def repair(pkt, challenges, corpus_dir):
        return copy.deepcopy(pkt)  # never concedes, never fixes

    state = run(patient_dir, assemble=assemble, review=review, repair=repair)

    assert state["round"] == 3, "cap must stop the loop at round 3 (got %r)" % state["round"]
    assert review_calls["n"] == 3, review_calls["n"]
    assert len(state["challenges"]) == 3
    assert all(len(c) == 1 for c in state["challenges"])
    assert state["terminal_state"] is not None, "terminal must be computed at the cap"

    # The last round file must carry the terminal state: /state's "latest" is
    # built from round files (never _final.json), so a cap-ended run would
    # otherwise show no verdict in the UI.
    with open(os.path.join(RUNS_DIR, "patientC_round3.json")) as f:
        round3 = json.load(f)
    assert round3["terminal_state"] == state["terminal_state"], (
        "cap path must re-persist the final round with terminal_state, got %r"
        % round3["terminal_state"])
    print("PASS scenario C: round cap enforced at 3, terminal computed = %s, "
          "round3 file carries terminal" % state["terminal_state"])


def scenario_d(tmp_root):
    """Two-strikes counts ROUNDS, not challenge objects: two same-round
    challenges on criterion 5 are ONE strike, so repair (which never concedes)
    gets its round-2 chance; the second-round re-challenge is strike two and
    forces the concession."""
    packet = _load_fixture("fixture_packet.json")
    challenge = _load_fixture("fixture_challenges.json")
    patient_dir = _make_patient_dir(tmp_root, "patientD")
    _track_runs("patientD")

    review_calls = {"n": 0}

    def assemble(rubric, corpus_dir):
        return _make_run_state(packet, patient_id="patientD")

    def review(pkt, rubric):
        review_calls["n"] += 1
        first = copy.deepcopy(challenge)[0]
        second = copy.deepcopy(challenge)[0]
        second["challenge_reason"] = "Second phrasing of the same objection."
        return [first, second]  # both target criterion 5, every round

    def repair(pkt, challenges, corpus_dir):
        return copy.deepcopy(pkt)  # never concedes on its own

    state = run(patient_dir, assemble=assemble, review=review, repair=repair)

    item5 = next(e for e in state["packet"] if e["criterion_id"] == 5)
    assert review_calls["n"] == 2, (
        "duplicate same-round challenges must NOT close the debate in round 1 "
        "(review called %d time(s))" % review_calls["n"])
    assert state["round"] == 2, state["round"]
    assert item5["status"] == "insufficient", item5["status"]
    assert "conceded after repeated challenge" in item5["reasoning"].lower(), \
        item5["reasoning"]
    assert state["terminal_state"] == "INSUFFICIENT_EVIDENCE", state["terminal_state"]
    print("PASS scenario D: same-round duplicate challenges = one strike; "
          "concession forced in round 2, not round 1")


def check_persistence(final_state_a):
    """After scenario A: round0..round1 + final files exist, valid JSON,
    schema keys (the concede-all early-exit ends the run at round 1)."""
    expected = [
        os.path.join(RUNS_DIR, "zz_testpatient_round0.json"),
        os.path.join(RUNS_DIR, "zz_testpatient_round1.json"),
        os.path.join(RUNS_DIR, "zz_testpatient_final.json"),
    ]
    for path in expected:
        assert os.path.exists(path), "missing persisted file: %s" % path
        with open(path, "r") as f:
            state = json.load(f)  # raises if invalid JSON
        assert RUN_STATE_KEYS.issubset(state.keys()), (
            "run_state schema keys missing in %s: %s" % (path, RUN_STATE_KEYS - set(state.keys())))
        assert isinstance(state["packet"], list)
        assert isinstance(state["challenges"], list)
        assert state["patient_id"] == "zz_testpatient"

    with open(os.path.join(RUNS_DIR, "zz_testpatient_final.json")) as f:
        final = json.load(f)
    assert final["terminal_state"] == final_state_a["terminal_state"]
    assert final["round"] == 1
    print("PASS persistence: round0-round1 + final exist, valid JSON, schema keys present")


def main():
    tmp_root = tempfile.mkdtemp(prefix="orch_test_")
    try:
        state_a = scenario_a(tmp_root)
        scenario_b(tmp_root)
        scenario_c(tmp_root)
        scenario_d(tmp_root)
        check_persistence(state_a)
        assert "agents" not in sys.modules, "agents.py was imported — stubs leaked"
        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        for path in _created_files:
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    main()
