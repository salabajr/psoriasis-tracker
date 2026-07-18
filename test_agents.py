"""Offline acceptance tests for agents.py.

Run:  .venv/bin/python test_agents.py

No network, no ANTHROPIC_API_KEY, no real tools.py: the Anthropic client and
the chart tools are both injected as mocks via agents.set_client / set_tools.
"""
import json
import os
import re
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import agents  # noqa: E402

FIXTURES = os.path.join(ROOT, "fixtures")
CORPUS_TINY = os.path.join(FIXTURES, "corpus_tiny")

with open(os.path.join(ROOT, "rubric.json")) as f:
    RUBRIC = json.load(f)
with open(os.path.join(FIXTURES, "fixture_packet.json")) as f:
    FIXTURE_PACKET = json.load(f)
with open(os.path.join(FIXTURES, "fixture_challenges.json")) as f:
    FIXTURE_CHALLENGES = json.load(f)

STATUS_VOCAB = {"worsening", "stable", "improving", "insufficient"}
DATE_FMT = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

class MockMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.responses) > 1:
            text = self.responses.pop(0)
        else:
            text = self.responses[0]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class MockClient:
    def __init__(self, *responses):
        self.messages = MockMessages(responses)


class MockTools:
    """Mock chart tools: substring-keyed search + explicit verify pairs."""

    def __init__(self, search_map, extra_verified=()):
        self.search_map = search_map  # list of (key_substring, results)
        self.verified = set(extra_verified)
        for _, results in search_map:
            for r in results:
                self.verified.add((r["doc_id"], r["quote"]))
        self.search_calls = []

    def chart_search(self, query, corpus_dir):
        self.search_calls.append(query)
        q = query.lower()
        for key, results in self.search_map:
            if key in q:
                return [dict(r) for r in results]
        return []

    def verify_quote(self, doc_id, quote, corpus_dir):
        return (doc_id, quote) in self.verified

    def rubric_lookup(self):
        return RUBRIC


PASI_HITS = [
    {"doc_id": "visit1_derm_note", "quote": "PASI 6.1, BSA 8%, IGA 2", "date": "2026-02-02", "score": 0.91},
    {"doc_id": "visit3_derm_note", "quote": "PASI 14.2, BSA 15%, IGA 3", "date": "2026-05-10", "score": 0.89},
    {"doc_id": "visit4_derm_note", "quote": "PASI 16.8, BSA 18%, IGA 3", "date": "2026-06-29", "score": 0.87},
]
DLQI_HITS = [
    {"doc_id": "visit1_derm_note", "quote": "DLQI 7", "date": "2026-02-02", "score": 0.9},
    {"doc_id": "visit3_derm_note", "quote": "DLQI 15", "date": "2026-05-10", "score": 0.88},
    {"doc_id": "visit4_derm_note", "quote": "DLQI 17", "date": "2026-06-29", "score": 0.86},
]
NAIL_HIT = [
    {"doc_id": "visit3_nursing_note", "quote": "onycholysis noted on two fingernails", "date": "2026-05-10", "score": 0.84},
]
REGIMEN_HITS = [
    {"doc_id": "visit3_derm_note", "quote": "start adalimumab 40 mg subcutaneously every 2 weeks, first dose administered today", "date": "2026-05-10", "score": 0.85},
    {"doc_id": "visit4_adherence_note", "quote": "doses due 2026-06-07 and 2026-06-21 were missed", "date": "2026-06-29", "score": 0.83},
]
CONFOUNDER_HITS = [
    {"doc_id": "visit4_adherence_note", "quote": "doses due 2026-06-07 and 2026-06-21 were missed", "date": "2026-06-29", "score": 0.82},
]

# Order matters: "onycholysis" must be matched before the bare "nail" key,
# which deliberately returns nothing so assemble is forced to reformulate.
ASSEMBLE_SEARCH_MAP = [
    ("pasi", PASI_HITS),
    ("dlqi", DLQI_HITS),
    ("onycholysis", NAIL_HIT),
    ("nail", []),
    ("confounder", CONFOUNDER_HITS),
    ("adherence", REGIMEN_HITS),
    ("regimen", REGIMEN_HITS),
]


def canned_assemble_response():
    packet = [
        {"criterion_id": 1, "criterion_text": RUBRIC[0]["text"], "status": "worsening",
         "evidence": [{k: h[k] for k in ("doc_id", "quote", "date")} for h in PASI_HITS],
         "reasoning": "PASI rose 6.1 -> 14.2 -> 16.8; BSA and IGA moved with it.", "rubric_ref": "1"},
        {"criterion_id": 2, "criterion_text": RUBRIC[1]["text"], "status": "worsening",
         "evidence": [{k: NAIL_HIT[0][k] for k in ("doc_id", "quote", "date")}],
         "reasoning": "New onycholysis at V3 indicates worsening nail involvement.", "rubric_ref": "2"},
        {"criterion_id": 3, "criterion_text": RUBRIC[2]["text"], "status": "worsening",
         "evidence": [{k: h[k] for k in ("doc_id", "quote", "date")} for h in DLQI_HITS],
         "reasoning": "DLQI rose from 7 to 15 to 17, beyond the 4-point threshold.", "rubric_ref": "3"},
        {"criterion_id": 4, "criterion_text": RUBRIC[3]["text"], "status": "insufficient",
         "evidence": [{k: h[k] for k in ("doc_id", "quote", "date")} for h in REGIMEN_HITS],
         "reasoning": "Two of four scheduled doses were missed; the trial is not adequate.", "rubric_ref": "4"},
        {"criterion_id": 5, "criterion_text": RUBRIC[4]["text"], "status": "worsening",
         "evidence": [{k: CONFOUNDER_HITS[0][k] for k in ("doc_id", "quote", "date")}],
         "reasoning": "Indices worsened despite starting adalimumab.", "rubric_ref": "5"},
    ]
    # wrapped in code fences to exercise the robust JSON parsing path
    return "```json\n" + json.dumps(packet) + "\n```"


# ---------------------------------------------------------------------------
# Schema validators (per contracts.md)
# ---------------------------------------------------------------------------

def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def validate_evidence(ev, where):
    check(isinstance(ev, dict), "%s: evidence entry not a dict" % where)
    check(set(ev.keys()) == {"doc_id", "quote", "date"},
          "%s: evidence keys %s" % (where, sorted(ev.keys())))
    check(isinstance(ev["doc_id"], str) and ev["doc_id"], "%s: bad doc_id" % where)
    check(isinstance(ev["quote"], str) and ev["quote"], "%s: bad quote" % where)
    check(bool(DATE_FMT.match(ev["date"])), "%s: bad date %r" % (where, ev["date"]))


def validate_packet_entry(entry, where):
    required = {"criterion_id", "criterion_text", "status", "evidence", "reasoning", "rubric_ref"}
    check(isinstance(entry, dict), "%s: entry not a dict" % where)
    check(required <= set(entry.keys()), "%s: missing keys %s" % (where, required - set(entry.keys())))
    check(isinstance(entry["criterion_id"], int), "%s: criterion_id not int" % where)
    check(entry["status"] in STATUS_VOCAB, "%s: bad status %r" % (where, entry["status"]))
    check(isinstance(entry["evidence"], list), "%s: evidence not a list" % where)
    for i, ev in enumerate(entry["evidence"]):
        validate_evidence(ev, "%s.evidence[%d]" % (where, i))
    check(isinstance(entry["reasoning"], str) and entry["reasoning"], "%s: bad reasoning" % where)
    check(isinstance(entry["rubric_ref"], str) and entry["rubric_ref"], "%s: bad rubric_ref" % where)


def validate_challenge(ch, where):
    required = {"criterion_id", "challenge_reason", "rubric_quote", "what_would_satisfy"}
    check(isinstance(ch, dict), "%s: challenge not a dict" % where)
    check(set(ch.keys()) == required, "%s: challenge keys %s" % (where, sorted(ch.keys())))
    check(isinstance(ch["criterion_id"], int), "%s: criterion_id not int" % where)
    for key in ("challenge_reason", "rubric_quote", "what_would_satisfy"):
        check(isinstance(ch[key], str) and ch[key].strip(), "%s: bad %s" % (where, key))


# ---------------------------------------------------------------------------
# Test 1 — assemble emits a schema-valid run_state
# ---------------------------------------------------------------------------

def test_assemble_run_state():
    tools = MockTools(ASSEMBLE_SEARCH_MAP)
    client = MockClient(canned_assemble_response())
    agents.set_tools(tools)
    agents.set_client(client)

    rs = agents.assemble(RUBRIC, CORPUS_TINY)

    check(set(rs.keys()) == {"patient_id", "round", "terminal_state", "packet",
                             "challenges", "search_trace"},
          "run_state keys: %s" % sorted(rs.keys()))
    check(rs["patient_id"] == os.path.basename(CORPUS_TINY),
          "patient_id must be basename of corpus_dir, got %r" % rs["patient_id"])
    check(rs["round"] == 0, "round must be 0")
    check(rs["terminal_state"] is None, "terminal_state must be None at round 0")
    check(rs["challenges"] == [], "challenges must be [] at round 0")

    check(len(rs["packet"]) == len(RUBRIC), "packet must cover all rubric items")
    ids = [e["criterion_id"] for e in rs["packet"]]
    check(ids == [item["id"] for item in RUBRIC], "packet order/ids: %s" % ids)
    for entry in rs["packet"]:
        validate_packet_entry(entry, "packet[cid=%s]" % entry.get("criterion_id"))
        # every evidence quote must have passed verify_quote before entering
        for ev in entry["evidence"]:
            check(tools.verify_quote(ev["doc_id"], ev["quote"], CORPUS_TINY),
                  "unverified evidence slipped into packet: %r" % ev)

    check(isinstance(rs["search_trace"], list) and len(rs["search_trace"]) == len(RUBRIC),
          "search_trace must have one entry per rubric item")
    for trace in rs["search_trace"]:
        check(set(trace.keys()) == {"criterion_id", "attempts", "resolved_in"},
              "trace keys: %s" % sorted(trace.keys()))
        check(isinstance(trace["criterion_id"], int), "trace criterion_id not int")
        check(isinstance(trace["attempts"], list) and trace["attempts"], "trace attempts empty")
        check(all(isinstance(a, str) and a for a in trace["attempts"]), "bad attempt strings")
        check(trace["resolved_in"] is None or isinstance(trace["resolved_in"], str),
              "resolved_in must be doc_id or None")

    # Invariant 2: the buried-finding reformulation happened inside assemble's
    # search loop and is recorded in the trace ("nail involvement" -> "onycholysis").
    trace2 = next(t for t in rs["search_trace"] if t["criterion_id"] == 2)
    check(trace2["attempts"][0] == "nail involvement",
          "criterion 2 initial query: %s" % trace2["attempts"])
    check("onycholysis" in trace2["attempts"],
          "reformulation to 'onycholysis' not recorded: %s" % trace2["attempts"])
    check(trace2["resolved_in"] == "visit3_nursing_note",
          "criterion 2 resolved_in: %r" % trace2["resolved_in"])
    check(len(client.messages.calls) == 1, "assemble should make exactly 1 model call")
    print("PASS  test_assemble_run_state")


# ---------------------------------------------------------------------------
# Test 2 — review: schema-valid challenge passes the guard; hallucinated
#          doc_id is stripped (with one retry, then [])
# ---------------------------------------------------------------------------

def test_review_valid_challenge():
    agents.set_tools(MockTools([]))  # review must not need tools at all
    client = MockClient(json.dumps(FIXTURE_CHALLENGES))
    agents.set_client(client)

    challenges = agents.review(FIXTURE_PACKET, RUBRIC)

    check(isinstance(challenges, list) and len(challenges) == 1,
          "expected exactly 1 surviving challenge, got %r" % challenges)
    validate_challenge(challenges[0], "challenges[0]")
    check(challenges[0]["criterion_id"] == 5, "challenge should target criterion 5")
    check(len(client.messages.calls) == 1, "valid challenge must not trigger a retry")

    # Invariant 1 sanity: the challenge references only packet-present material
    packet_doc_ids = {ev["doc_id"] for e in FIXTURE_PACKET for ev in e["evidence"]}
    text = challenges[0]["challenge_reason"] + " " + challenges[0]["what_would_satisfy"]
    for tok in re.findall(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b", text):
        check(tok in packet_doc_ids, "challenge cites unknown doc_id %r" % tok)
    print("PASS  test_review_valid_challenge")


def test_review_guard_strips_hallucinated_docid():
    bad_challenge = [{
        "criterion_id": 5,
        "challenge_reason": "The chart's secret_note documents an infection that "
                            "the packet ignores.",
        "rubric_quote": RUBRIC[4]["text"],
        "what_would_satisfy": "Quote secret_note showing the infection was excluded.",
    }]
    # Model insists on the hallucination twice: guard strips, retries once,
    # strips again, then gives up and returns [].
    client = MockClient(json.dumps(bad_challenge), json.dumps(bad_challenge))
    agents.set_client(client)
    agents.set_tools(MockTools([]))

    challenges = agents.review(FIXTURE_PACKET, RUBRIC)

    check(challenges == [], "hallucinated challenge must be stripped, got %r" % challenges)
    check(len(client.messages.calls) == 2,
          "guard must retry exactly once (got %d calls)" % len(client.messages.calls))
    retry_text = client.messages.calls[1]["messages"][0]["content"]
    check("secret_note" in retry_text and "REJECTED" in retry_text,
          "retry must explain the violation to the model")
    print("PASS  test_review_guard_strips_hallucinated_docid")


def test_review_guard_strips_fabricated_rubric_quote():
    bad_challenge = [{
        "criterion_id": 5,
        "challenge_reason": "The worsening claim leaves the confounder question "
                            "unresolved.",
        "rubric_quote": "All confounders must be excluded by laboratory testing "
                        "before any worsening call.",  # nowhere in the rubric
        "what_would_satisfy": "A chart statement excluding non-adherence as the "
                              "driver of the worsening.",
    }]
    # Model fabricates the same rubric text twice: guard strips, retries once,
    # strips again, then gives up and returns [].
    client = MockClient(json.dumps(bad_challenge), json.dumps(bad_challenge))
    agents.set_client(client)
    agents.set_tools(MockTools([]))

    challenges = agents.review(FIXTURE_PACKET, RUBRIC)

    check(challenges == [], "fabricated rubric_quote must be stripped, got %r" % challenges)
    check(len(client.messages.calls) == 2,
          "guard must retry exactly once (got %d calls)" % len(client.messages.calls))
    retry_text = client.messages.calls[1]["messages"][0]["content"]
    check("rubric_quote" in retry_text and "REJECTED" in retry_text,
          "retry must explain the fabricated rubric quote to the model")
    # A verbatim rubric quote (any case/whitespace) still passes the guard.
    good = [dict(FIXTURE_CHALLENGES[0], rubric_quote=RUBRIC[4]["text"].upper())]
    agents.set_client(MockClient(json.dumps(good)))
    kept = agents.review(FIXTURE_PACKET, RUBRIC)
    check(len(kept) == 1, "verbatim-modulo-case rubric_quote must survive, got %r" % kept)
    print("PASS  test_review_guard_strips_fabricated_rubric_quote")


def test_review_stand_down():
    client = MockClient("[]")
    agents.set_client(client)
    challenges = agents.review(FIXTURE_PACKET, RUBRIC)
    check(challenges == [], "stand-down [] must pass through unchanged")
    check(len(client.messages.calls) == 1, "stand-down must not trigger a retry")
    print("PASS  test_review_stand_down")


# ---------------------------------------------------------------------------
# Test 3 — repair: chart cannot satisfy -> item 5 conceded as insufficient
# ---------------------------------------------------------------------------

def test_repair_concession():
    # Chart cannot satisfy the challenge: every repair search returns [].
    # Verify pairs cover the existing fixture-packet evidence so it survives.
    fixture_pairs = [(ev["doc_id"], ev["quote"])
                     for e in FIXTURE_PACKET for ev in e["evidence"]]
    tools = MockTools([], extra_verified=fixture_pairs)
    # Model misbehaves and keeps item 5 as "worsening": the code-level rule
    # must still force the concession.
    model_packet = json.dumps(FIXTURE_PACKET)
    client = MockClient(model_packet)
    agents.set_tools(tools)
    agents.set_client(client)

    patched = agents.repair(FIXTURE_PACKET, FIXTURE_CHALLENGES, CORPUS_TINY)

    check(isinstance(patched, list) and len(patched) == 5, "patched packet must keep 5 items")
    for entry in patched:
        validate_packet_entry(entry, "patched[cid=%s]" % entry.get("criterion_id"))
    item5 = next(e for e in patched if e["criterion_id"] == 5)
    check(item5["status"] == "insufficient",
          "unsatisfiable challenge must yield status 'insufficient', got %r" % item5["status"])
    reasoning = item5["reasoning"].lower()
    check("conced" in reasoning, "reasoning must state the concession: %r" % item5["reasoning"])
    wws = FIXTURE_CHALLENGES[0]["what_would_satisfy"]
    check(wws in item5["reasoning"] or "settle" in reasoning,
          "reasoning must state what documentation would settle it: %r" % item5["reasoning"])
    check(tools.search_calls, "repair must run fresh chart_search attempts")
    check(len(tools.search_calls) <= 3, "repair capped at 3 attempts per challenge")
    # Unchallenged items survive with verified evidence intact
    item1 = next(e for e in patched if e["criterion_id"] == 1)
    check(item1["status"] == "worsening" and len(item1["evidence"]) == 3,
          "unchallenged item 1 must be preserved")
    print("PASS  test_repair_concession")


# ---------------------------------------------------------------------------
# Test 4 — request structure: cache_control on the stable prefix, variable
#          content only in the user tail
# ---------------------------------------------------------------------------

def test_request_structure_cache_prefix():
    check(config.CACHE_PREFIX is True, "config.CACHE_PREFIX expected True for this test")
    client = MockClient(json.dumps(FIXTURE_CHALLENGES))
    agents.set_client(client)
    agents.review(FIXTURE_PACKET, RUBRIC)

    call = client.messages.calls[0]
    check(call["model"] == config.MODEL, "model must come from config.MODEL")
    if getattr(config, "NO_TEMPERATURE", False):
        check("temperature" not in call,
              "temperature must be OMITTED on modern-surface models")
    else:
        check(call["temperature"] == config.TEMPERATURE,
              "temperature must come from config")

    system = call["system"]
    check(isinstance(system, list) and system, "system must be a list of blocks")
    for block in system:
        check(block.get("type") == "text", "system blocks must be text blocks")
    check(system[-1].get("cache_control") == {"type": "ephemeral"},
          "final stable system block must carry cache_control ephemeral")

    # Stable prefix contains the rubric...
    system_text = " ".join(b["text"] for b in system)
    check(RUBRIC[4]["text"] in system_text, "rubric must live in the cached stable prefix")
    # ...but never the per-call variable content (the packet)
    marker = "PASI 16.8, BSA 18%, IGA 3"  # packet-specific quote
    check(marker not in system_text,
          "variable packet content leaked into the cached system prefix")
    messages = call["messages"]
    check(len(messages) == 1 and messages[0]["role"] == "user", "one user message expected")
    check(marker in messages[0]["content"], "packet must ride in the user tail")

    # Same discipline on the assemble path (rubric+corpus stable, candidates in tail)
    tools = MockTools(ASSEMBLE_SEARCH_MAP)
    client2 = MockClient(canned_assemble_response())
    agents.set_tools(tools)
    agents.set_client(client2)
    agents.assemble(RUBRIC, CORPUS_TINY)
    call2 = client2.messages.calls[0]
    check(call2["system"][-1].get("cache_control") == {"type": "ephemeral"},
          "assemble stable prefix must carry cache_control")
    sys_text2 = " ".join(b["text"] for b in call2["system"])
    check(RUBRIC[0]["text"] in sys_text2, "assemble prefix must contain the rubric")
    check("visit3_nursing_note" in sys_text2, "assemble prefix must contain the corpus manifest")
    check("onycholysis noted on two fingernails" not in sys_text2,
          "search candidates must not leak into the cached prefix")
    check("onycholysis noted on two fingernails" in call2["messages"][0]["content"],
          "search candidates must ride in the user tail")
    print("PASS  test_request_structure_cache_prefix")


# ---------------------------------------------------------------------------

def main():
    tests = [
        test_assemble_run_state,
        test_review_valid_challenge,
        test_review_guard_strips_hallucinated_docid,
        test_review_guard_strips_fabricated_rubric_quote,
        test_review_stand_down,
        test_repair_concession,
        test_request_structure_cache_prefix,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print("FAIL  %s: %s" % (t.__name__, exc))
    if failures:
        print("\n%d/%d tests FAILED" % (failures, len(tests)))
        sys.exit(1)
    print("\nAll %d tests passed (offline; mocked client + mocked tools)." % len(tests))


if __name__ == "__main__":
    main()
