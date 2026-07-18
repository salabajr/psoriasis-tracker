"""Adversarial agents for the chart-review pipeline.

Three modes, frozen signatures (see contracts.md):

    assemble(rubric, corpus_dir) -> run_state (round 0)
    review(packet, rubric)       -> challenges (Agent B: packet + rubric ONLY)
    repair(packet, challenges, corpus_dir) -> patched packet

All Claude traffic goes through one helper, `_call_claude`, which imports
MODEL/TEMPERATURE from config, marks the stable prefix (system prompt +
rubric [+ corpus manifest]) with cache_control when config.CACHE_PREFIX, and
puts only the per-call variable content in the user tail.

Test seams (tests are offline and must never touch the real tools/network):
    set_tools(mock)   -> replaces the module-level TOOLS indirection
    set_client(mock)  -> replaces the module-level Anthropic client
"""
from __future__ import annotations

import json
import os
import re
import time

import config

# ---------------------------------------------------------------------------
# Tool + client indirection (tests inject mocks; never import real tools there)
# ---------------------------------------------------------------------------

try:  # tools.py is written by a sibling workstream and may not exist yet
    import tools as TOOLS  # noqa: N812
except Exception:  # pragma: no cover - absence/breakage of sibling module
    TOOLS = None

import events  # no-op unless orchestrator started an event stream

_client = None

STATUS_VOCAB = {"worsening", "stable", "improving", "insufficient"}

_PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

_DATE_FMT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# doc_id-shaped token: snake_case with at least one underscore (e.g. visit3_nursing_note)
_DOCID_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
_QUOTED_RE = re.compile(r'"([^"]{8,}?)"|“([^”]{8,}?)”')


def set_tools(mock):
    """Inject a tools implementation (tests pass a mock; runtime may pass tools)."""
    global TOOLS
    TOOLS = mock


def set_client(client):
    """Inject an Anthropic-compatible client (tests pass a mock)."""
    global _client
    _client = client


def _get_tools():
    if TOOLS is None:
        raise RuntimeError(
            "No tools available: real tools.py could not be imported and "
            "set_tools() was never called."
        )
    return TOOLS


def _get_client():
    global _client
    if _client is None:  # pragma: no cover - live path, exercised in Phase 2
        import anthropic

        _client = anthropic.Anthropic()
    return _client


# ---------------------------------------------------------------------------
# Shared Claude helper
# ---------------------------------------------------------------------------

def _load_prompt(name: str) -> str:
    with open(os.path.join(_PROMPT_DIR, name + ".txt"), "r", encoding="utf-8") as f:
        return f.read()


def _corpus_manifest(corpus_dir: str) -> str:
    try:
        names = sorted(
            os.path.splitext(f)[0]
            for f in os.listdir(corpus_dir)
            if f.endswith(".md")
        )
    except OSError:
        names = []
    return "\n".join(names) if names else "(no document listing available)"


def _system_blocks(prompt_name: str, **subs) -> list:
    """Build the stable system prefix for one agent mode.

    The prompt file (with rubric [+ corpus manifest] substituted) is the stable
    prefix. When config.CACHE_PREFIX is set, the final stable block carries
    cache_control {"type": "ephemeral"}; the per-call variable content lives
    only in the user message tail — never interleaved into the cached block.
    """
    text = _load_prompt(prompt_name)
    for key, value in subs.items():
        text = text.replace("{" + key + "}", value)
    block = {"type": "text", "text": text}
    if getattr(config, "CACHE_PREFIX", False):
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def _extract_text(response) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "text") == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _parse_json(text: str):
    """Parse model output robustly: strip code fences / prose, load JSON."""
    if text is None:
        raise ValueError("empty model response")
    s = text.strip()
    # strip markdown code fences
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    # clamp to the outermost JSON payload
    starts = [i for i in (s.find("["), s.find("{")) if i != -1]
    if not starts:
        raise ValueError("no JSON payload in model response")
    start = min(starts)
    end = max(s.rfind("]"), s.rfind("}"))
    if end <= start:
        raise ValueError("unterminated JSON payload in model response")
    return json.loads(s[start : end + 1])


_STREAM_SEQ = 0


def _stream_message(client, kwargs: dict, actor: str):
    """Stream one model call, emitting throttled "delta" transcript events.

    Each delta carries a stream id (sid) so the UI can grow one bubble per
    call; the final delta has done=True. Returns the final Message, or None
    when this client can't stream (mocked clients, old SDKs) — the caller
    then falls back to the blocking path.
    """
    global _STREAM_SEQ
    try:
        if getattr(config, "IS_FABLE", False):
            cm = client.beta.messages.stream(
                betas=["server-side-fallback-2026-06-01"],
                fallbacks=[{"model": config.FALLBACK_MODEL}],
                **kwargs,
            )
        else:
            cm = client.messages.stream(temperature=config.TEMPERATURE, **kwargs)
    except (AttributeError, TypeError):
        return None

    _STREAM_SEQ += 1
    sid = "s%d" % _STREAM_SEQ
    buf, last = [], [0.0]

    def _flush(done=False):
        chunk = "".join(buf)
        if chunk or done:
            events.emit(actor, "delta", chunk, sid=sid, done=done)
            del buf[:]
            last[0] = time.time()

    with cm as stream:
        for piece in stream.text_stream:
            buf.append(piece)
            if sum(map(len, buf)) >= 150 or time.time() - last[0] >= 0.4:
                _flush()
        _flush(done=True)
        return stream.get_final_message()


def _create_message(client, kwargs: dict, actor: str = None):
    """Model-aware messages.create (every Claude call funnels through here).

    Fable 5: no temperature (rejected), server-side refusal fallback via the
    beta endpoint. Other models: plain create with config.TEMPERATURE.

    When an actor is given and a demo event stream is open, the call streams
    and the transcript sees the output live; any streaming failure falls back
    to the blocking call (the demo must never take down the pipeline).
    """
    if actor and events.is_active():
        try:
            msg = _stream_message(client, kwargs, actor)
            if msg is not None:
                return msg
        except Exception:
            pass
    if getattr(config, "IS_FABLE", False):
        try:
            return client.beta.messages.create(
                betas=["server-side-fallback-2026-06-01"],
                fallbacks=[{"model": config.FALLBACK_MODEL}],
                **kwargs,
            )
        except AttributeError:
            pass  # mocked client without a beta surface (unit tests)
    return client.messages.create(temperature=config.TEMPERATURE, **kwargs)


def _call_claude(system_blocks: list, user_content: str, actor: str = None):
    """One shared entry point for every Claude call in this module.

    Requests JSON output; parses it robustly, retrying exactly once on a
    parse error with an explicit "JSON only" nudge appended to the user tail.
    """
    client = _get_client()

    def _go(content) -> str:
        kwargs = dict(
            model=config.MODEL,
            max_tokens=getattr(config, "MAX_TOKENS", 4096),
            system=system_blocks,
            messages=[{"role": "user", "content": content}],
        )
        return _extract_text(_create_message(client, kwargs, actor=actor))

    text = _go(user_content)
    try:
        return _parse_json(text)
    except (ValueError, json.JSONDecodeError):
        nudge = ("Your previous reply was not valid JSON. Respond with ONLY the "
                 "JSON payload — no prose, no code fences.")
        if isinstance(user_content, str):
            retry_content = user_content + "\n\n" + nudge
        else:  # multimodal content blocks (vision)
            retry_content = list(user_content) + [{"type": "text", "text": nudge}]
        return _parse_json(_go(retry_content))


# ---------------------------------------------------------------------------
# Evidence hygiene (shared by assemble/repair)
# ---------------------------------------------------------------------------

def _clean_evidence(evidence, corpus_dir, allowed_pairs=None):
    """Keep only well-shaped, verify_quote-verified evidence entries.

    Every quote must pass verify_quote BEFORE it enters a packet (contract #4).
    When allowed_pairs is given, evidence must additionally come from the
    known candidate set (the model may not smuggle in unseen quotes).
    """
    tools_mod = _get_tools()
    out, seen = [], set()
    for ev in evidence or []:
        if not isinstance(ev, dict):
            continue
        doc_id, quote, date = ev.get("doc_id"), ev.get("quote"), ev.get("date")
        if not (isinstance(doc_id, str) and isinstance(quote, str) and isinstance(date, str)):
            continue
        if not _DATE_FMT.match(date):
            continue
        if allowed_pairs is not None and (doc_id, quote) not in allowed_pairs:
            continue
        if (doc_id, quote, date) in seen:
            continue
        if doc_id.startswith("images/"):
            # Photo observation: not a verbatim chart quote, so verify_quote
            # does not apply. It is legal ONLY if it exactly matches a finding
            # Agent A generated from that image (enforced via allowed_pairs);
            # without a candidate set to match against, it is rejected.
            if allowed_pairs is None:
                continue
        else:
            try:
                if not tools_mod.verify_quote(doc_id, quote, corpus_dir):
                    continue
            except Exception:
                continue
        seen.add((doc_id, quote, date))
        out.append({"doc_id": doc_id, "quote": quote, "date": date})
    return out


def _normalize_entry(raw, rubric_item, corpus_dir, allowed_pairs):
    """Coerce one model-emitted packet entry into the frozen schema."""
    rid = rubric_item["id"]
    entry = raw if isinstance(raw, dict) else {}
    status = entry.get("status")
    if status not in STATUS_VOCAB:
        status = "insufficient"
    evidence = _clean_evidence(entry.get("evidence"), corpus_dir, allowed_pairs)
    reasoning = entry.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        reasoning = "No reasoning provided."
    if not evidence and status != "insufficient":
        status = "insufficient"
        reasoning += (
            " [No verifiable chart evidence survived verification for this "
            "criterion, so its status cannot be asserted.]"
        )
    rubric_ref = entry.get("rubric_ref")
    if not isinstance(rubric_ref, str) or not rubric_ref.strip():
        rubric_ref = str(rid)
    out = {
        "criterion_id": rid,
        "criterion_text": rubric_item["text"],
        "status": status,
        "evidence": evidence,
        "reasoning": reasoning,
        "rubric_ref": rubric_ref,
    }
    gap = entry.get("gap")
    if isinstance(gap, str) and gap.strip():
        out["gap"] = gap.strip()[:300]
    return out


# ---------------------------------------------------------------------------
# ASSEMBLE — Agent A, round 0
# ---------------------------------------------------------------------------

# Lightweight reformulation heuristics for the search loop. The buried-finding
# reformulation (e.g. "nail involvement" -> "onycholysis") happens HERE, inside
# assemble's search loop, and is recorded in run_state["search_trace"]. It is
# never a review challenge (invariant #2).
_SYNONYMS = {
    "nail involvement": ["onycholysis"],
    "PASI BSA IGA": ["psoriasis severity score", "IGA severity assessment"],
    "DLQI": ["dermatology life quality index", "quality of life score"],
    "current regimen adherence doses": ["missed doses", "medication adherence"],
    "confounder missed doses infection steroid seasonal": [
        "missed doses",
        "infection steroid taper seasonal flare",
    ],
}


def _initial_query(rubric_item: dict) -> str:
    text = (rubric_item.get("text", "") + " " + rubric_item.get("how", "")).lower()
    if "pasi" in text:
        return "PASI BSA IGA"
    if "dlqi" in text:
        return "DLQI"
    if "nail" in text:
        return "nail involvement"
    if "confounder" in text:
        return "confounder missed doses infection steroid seasonal"
    if "regimen" in text or "treatment" in text:
        return "current regimen adherence doses"
    return rubric_item.get("text", "")[:80]


def _eg_terms(rubric_item: dict) -> list:
    """Fallback reformulations: parenthetical 'e.g., ...' examples in the rubric."""
    how = rubric_item.get("how", "")
    terms = []
    for group in re.findall(r"e\.g\.,?\s*([^)]+)\)", how):
        for term in group.split(","):
            term = term.strip(" .")
            if term and term.lower() not in (t.lower() for t in terms):
                terms.append(term)
    return terms


def _reformulations(rubric_item: dict, initial_query: str) -> list:
    chain = list(_SYNONYMS.get(initial_query, []))
    for term in _eg_terms(rubric_item):
        if term.lower() != initial_query.lower() and term not in chain:
            chain.append(term)
    return chain


def _search_criterion(rubric_item: dict, corpus_dir: str):
    """Up to 3 chart_search attempts for one rubric item.

    Returns (trace_entry, verified_candidates). Every attempt string is
    recorded; resolved_in is the doc_id that satisfied the query, or None.
    """
    tools_mod = _get_tools()
    queries = [_initial_query(rubric_item)] + _reformulations(
        rubric_item, _initial_query(rubric_item)
    )
    attempts, candidates, resolved_in = [], [], None
    seen = set()
    for query in queries[:3]:
        # Keep reformulating until the evidence spans at least two distinct
        # documents (triangulation) or the query chain is exhausted. A single
        # criterion often has facets in different notes (e.g. a therapy start
        # in one note and its adherence record in another).
        if len({c["doc_id"] for c in candidates}) >= 2:
            break
        already_resolved = resolved_in is not None
        added = 0
        attempt_docs = set()
        attempts.append(query)
        try:
            results = tools_mod.chart_search(query, corpus_dir) or []
        except Exception:
            results = []
        for r in results:
            if not isinstance(r, dict):
                continue
            doc_id, quote = r.get("doc_id"), r.get("quote")
            date = r.get("date", "")
            if not (isinstance(doc_id, str) and isinstance(quote, str)):
                continue
            if (doc_id, quote) in seen:
                continue
            try:
                if tools_mod.verify_quote(doc_id, quote, corpus_dir):
                    seen.add((doc_id, quote))
                    candidates.append({"doc_id": doc_id, "quote": quote, "date": date})
                    added += 1
                    attempt_docs.add(doc_id)
                    if resolved_in is None:
                        resolved_in = doc_id
            except Exception:
                continue
        # Per-query events were too noisy for the demo transcript; the full
        # query-by-query trail lives in the Search trace panel (run_state).
        if already_resolved and added == 0:
            # A post-resolution triangulation query that contributed nothing
            # does not belong in the trace of attempts that shaped the packet.
            attempts.pop()
    events.emit(
        "A", "tick", "Criterion %s: evidence gathered" % rubric_item["id"],
        criterion=rubric_item["id"], resolved_in=resolved_in,
    )
    trace = {
        "criterion_id": rubric_item["id"],
        "attempts": attempts,
        "resolved_in": resolved_in,
    }
    return trace, candidates


def _photo_findings(corpus_dir: str) -> list:
    """Agent A examines chart photographs with one batched vision call.

    Returns [{"doc_id": "images/<file>", "quote": <observation>, "date": ...}].
    Empty when the chart has no images/ or no client is available. Findings
    are Agent A's own labeled observations, not verbatim chart quotes; they
    may enter the packet only as exact matches (enforced in _clean_evidence).
    """
    import base64
    import glob as _glob

    paths = sorted(
        _glob.glob(os.path.join(corpus_dir, "images", "*.jpg"))
        + _glob.glob(os.path.join(corpus_dir, "images", "*.png"))
    )
    if not paths:
        return []
    if _client is None and not os.environ.get("ANTHROPIC_API_KEY"):
        return []

    # Map visit prefixes to dates using the notes themselves.
    visit_dates = {}
    for md in _glob.glob(os.path.join(corpus_dir, "*.md")):
        stem = os.path.basename(md).split("_")[0]
        if stem in visit_dates:
            continue
        try:
            with open(md, "r", encoding="utf-8") as f:
                m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", f.read())
            if m:
                visit_dates[stem] = m.group(1)
        except OSError:
            continue

    content = []
    rels = []
    for path in paths:
        rel = "images/" + os.path.basename(path)
        rels.append(rel)
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        media = "image/png" if path.endswith(".png") else "image/jpeg"
        content.append({"type": "text", "text": "Photograph — chart file %s:" % rel})
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": media,
                                   "data": b64}})
    content.append({"type": "text",
                    "text": "Write one observation per photograph. "
                            "JSON array only."})
    try:
        raw = _call_claude(_system_blocks("vision"), content, actor="A")
    except Exception:
        return []
    findings = []
    by_file = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                by_file[str(item.get("file", ""))] = str(item.get("finding", "")).strip()
    for rel in rels:
        finding = by_file.get(rel, "")
        if not finding or finding == "image not interpretable":
            continue
        visit = os.path.basename(rel).split("_")[0]
        entry = {"doc_id": rel, "quote": finding,
                 "date": visit_dates.get(visit, "")}
        findings.append(entry)
    if findings:
        # One line for the whole photo review — per-image captions stay in
        # the data payload, not the demo transcript.
        events.emit("A", "photo",
                    "\U0001F4F7 We looked at the %d photograph(s) filed to the "
                    "chart and noted what's visible in each." % len(findings),
                    files=[f["doc_id"] for f in findings],
                    findings=[f["quote"] for f in findings])
    return findings


def assemble(rubric: list, corpus_dir: str) -> dict:
    """Build the round-0 run_state: search the chart, then draft the packet."""
    global _LAST_PATIENT_ID
    _LAST_PATIENT_ID = os.path.basename(os.path.normpath(corpus_dir))
    events.emit("A", "phase",
                "We're reading the patient's chart, looking for evidence on "
                "each of the %d rubric questions. We only keep quotes we can "
                "verify word-for-word against the notes." % len(rubric))
    photo_findings = _photo_findings(corpus_dir)
    search_trace = []
    candidates_by_id = {}
    for item in rubric:
        trace, candidates = _search_criterion(item, corpus_dir)
        search_trace.append(trace)
        candidates_by_id[item["id"]] = candidates
    n_queries = sum(len(t.get("attempts", [])) for t in search_trace)
    n_quotes = sum(len(v) for v in candidates_by_id.values())
    events.emit("A", "info",
                "We searched the chart %d different ways and verified %d exact "
                "quotes to work from. (The full search trail is in the panel "
                "below.)" % (n_queries, n_quotes),
                queries=n_queries, quotes=n_quotes)

    system_blocks = _system_blocks(
        "assemble",
        rubric_json=json.dumps(rubric, indent=2),
        corpus_manifest=_corpus_manifest(corpus_dir),
    )
    user_payload = {
        "criteria": [
            {
                "criterion_id": item["id"],
                "criterion_text": item["text"],
                "how": item.get("how", ""),
                "candidates": candidates_by_id[item["id"]],
            }
            for item in rubric
        ],
        "photo_findings": photo_findings,
    }
    user_content = (
        "Verified chart-search candidates per rubric criterion, plus your own "
        "photo_findings from examining the chart photographs (usable as "
        "evidence for any relevant criterion, quoted exactly, doc_id = the "
        "image file):\n"
        + json.dumps(user_payload, indent=2)
        + "\n\nBuild the evidence packet now. JSON array only."
    )
    events.emit("A", "info", "Now we're writing up what we found into the "
                             "evidence packet…")
    raw = _call_claude(system_blocks, user_content, actor="A")
    if isinstance(raw, dict):
        raw = raw.get("packet") or raw.get("entries") or []
    by_id = {}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                try:
                    by_id[int(entry.get("criterion_id"))] = entry
                except (TypeError, ValueError):
                    continue

    photo_pairs = {(p["doc_id"], p["quote"]) for p in photo_findings}
    packet = []
    for item in rubric:
        allowed = {(c["doc_id"], c["quote"]) for c in candidates_by_id[item["id"]]}
        allowed |= photo_pairs
        packet.append(
            _normalize_entry(by_id.get(item["id"]), item, corpus_dir, allowed)
        )

    for entry in packet:
        if entry.get("gap"):
            events.emit(
                "A", "gap",
                "⚠️ The chart is missing something for question %s: %s"
                % (entry["criterion_id"], entry["gap"]),
                criterion=entry["criterion_id"],
            )
    events.emit(
        "A", "packet",
        "Our evidence packet is ready — handing it to Agent B for review.",
        statuses={str(e["criterion_id"]): e["status"] for e in packet},
    )
    return {
        "patient_id": os.path.basename(os.path.normpath(corpus_dir)),
        "round": 0,
        "terminal_state": None,
        "packet": packet,
        "challenges": [],
        "search_trace": search_trace,
    }


# ---------------------------------------------------------------------------
# REVIEW — Agent B (packet + rubric ONLY; no chart access)
# ---------------------------------------------------------------------------

def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _packet_grounding(packet: list, rubric: list):
    """Material Agent B is allowed to reference: what the packet/rubric contain."""
    doc_ids, dates, blob_parts = set(), set(), []
    for entry in packet or []:
        if not isinstance(entry, dict):
            continue
        for key in ("criterion_text", "reasoning", "rubric_ref", "status"):
            blob_parts.append(str(entry.get(key, "")))
        for ev in entry.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            doc_ids.add(str(ev.get("doc_id", "")))
            blob_parts.append(str(ev.get("quote", "")))
            date = str(ev.get("date", ""))
            dates.add(date)
            dates.update(_DATE_RE.findall(str(ev.get("quote", ""))))
    rubric_blob = " ".join(
        str(item.get("id", "")) + " " + item.get("text", "") + " " + item.get("how", "")
        for item in (rubric or [])
    )
    packet_blob = " ".join(blob_parts)
    # dates mentioned in packet reasoning/criterion text also count as packet-present
    dates.update(_DATE_RE.findall(packet_blob))
    return doc_ids, dates, packet_blob, rubric_blob


def _challenge_violations(challenge: dict, doc_ids, dates, packet_blob, rubric_blob):
    """Invariant-1 guard: flag references to material absent from the packet.

    Only challenge_reason and what_would_satisfy are scanned (per spec).
    """
    violations = []
    text = " ".join(
        str(challenge.get(k, "")) for k in ("challenge_reason", "what_would_satisfy")
    )
    allowed_blob = _norm_ws(packet_blob + " " + rubric_blob)
    for token in set(_DOCID_RE.findall(text)):
        if token in doc_ids:
            continue
        if token.lower() in allowed_blob:
            continue
        violations.append("references doc_id '%s' not present in the packet" % token)
    for date in set(_DATE_RE.findall(text)):
        if date not in dates:
            violations.append("references date %s not present in packet evidence" % date)
    for match in _QUOTED_RE.finditer(text):
        span = next((g for g in match.groups() if g), "")
        if span and _norm_ws(span) not in allowed_blob:
            violations.append(
                "quotes text not present in the packet: '%s...'" % span[:40]
            )
    return violations


def _guard_challenges(raw, packet: list, rubric: list, extra_blob: str = ""):
    """Validate + filter model challenges. Returns (kept, violations, attempted).

    extra_blob is grounding material beyond the packet (e.g. pharmacy dispense
    bundles B actually retrieved this round) — dates/spans from it are legal.
    """
    if isinstance(raw, dict):
        raw = raw.get("challenges") or []
    if not isinstance(raw, list):
        return [], ["model output was not a JSON array of challenges"], raw is not None
    attempted = len(raw) > 0
    packet_ids = set()
    for entry in packet or []:
        try:
            packet_ids.add(int(entry.get("criterion_id")))
        except (TypeError, ValueError, AttributeError):
            continue
    doc_ids, dates, packet_blob, rubric_blob = _packet_grounding(packet, rubric)
    if extra_blob:
        packet_blob = packet_blob + " " + _norm_ws(extra_blob)
        dates = set(dates) | set(_DATE_RE.findall(extra_blob))
    kept, violations = [], []
    for ch in raw:
        if not isinstance(ch, dict):
            violations.append("challenge is not an object")
            continue
        try:
            cid = int(ch.get("criterion_id"))
        except (TypeError, ValueError):
            violations.append("challenge has no valid criterion_id")
            continue
        if cid not in packet_ids:
            violations.append("challenge targets criterion_id %s not in packet" % cid)
            continue
        fields = {}
        bad_shape = False
        for key in ("challenge_reason", "rubric_quote", "what_would_satisfy"):
            value = ch.get(key)
            if not isinstance(value, str) or not value.strip():
                violations.append("challenge missing '%s'" % key)
                bad_shape = True
                break
            fields[key] = value
        if bad_shape:
            continue
        vios = _challenge_violations(ch, doc_ids, dates, packet_blob, rubric_blob)
        if vios:
            violations.extend(vios)
            continue
        # A challenge belongs to the rubric item whose rule it quotes: if the
        # quoted rubric text lives in exactly one other item, remap. (Keeps
        # confounder contradictions filed on the confounder criterion, not on
        # the measurement item they conflict with.)
        quote_norm = _norm_ws(fields["rubric_quote"])
        owners = [
            item["id"] for item in rubric
            if quote_norm and quote_norm in
            _norm_ws(str(item.get("text", "")) + " " + str(item.get("how", "")))
        ]
        if len(owners) == 1 and owners[0] != cid and owners[0] in packet_ids:
            cid = owners[0]
        kept_ch = {
            "criterion_id": cid,
            "challenge_reason": fields["challenge_reason"],
            "rubric_quote": fields["rubric_quote"],
            "what_would_satisfy": fields["what_would_satisfy"],
        }
        if isinstance(ch.get("external_evidence"), list):
            kept_ch["external_evidence"] = ch["external_evidence"]
        kept.append(kept_ch)
    return kept, violations, attempted


def _trunc(s, n: int) -> str:
    s = str(s or "")
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0] + " …"


def _emit_review_result(challenges: list) -> list:
    if not challenges:
        events.emit("B", "standdown",
                    "We have no objections — the evidence holds up under the "
                    "rubric.")
    for c in challenges:
        events.emit(
            "B", "challenge",
            "On question %s: %s"
            % (c.get("criterion_id"), _trunc(c.get("challenge_reason"), 320)),
            criterion=c.get("criterion_id"),
            rubric_quote=_trunc(c.get("rubric_quote"), 300),
            what_would_satisfy=_trunc(c.get("what_would_satisfy"), 400),
            external_evidence=c.get("external_evidence", []),
        )
    return challenges


# ---------------------------------------------------------------------------
# Agent B's pharmacy verification tool (B's ONLY tool; A never gets it)
# ---------------------------------------------------------------------------

PHARMACY_TOOL_SCHEMA = {
    "name": "pharmacy_lookup",
    "description": (
        "Look up the patient's medication fill history from the pharmacy "
        "dispense feed. Returns a FHIR MedicationDispense bundle: dispense "
        "dates, medications, quantities, and days supplied. Call only when "
        "an adherence question bears on a claim under review."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "patient_id": {"type": "string",
                           "description": "The patient id under review."}
        },
        "required": ["patient_id"],
    },
}

_LAST_PATIENT_ID = None   # set by assemble(); review() has no patient_id param
_TOOL_TRACE = []          # drained by orchestrator via consume_tool_trace()


def consume_tool_trace() -> list:
    """Return and clear tool-trace entries accumulated since the last call."""
    global _TOOL_TRACE
    trace, _TOOL_TRACE = _TOOL_TRACE, []
    return trace


def _review_round(system_blocks: list, user_content: str, patient_id: str):
    """Agent B's tool-use loop: at most 3 model calls, at most 2 pharmacy
    lookups per review round. Returns (parsed_json_or_None, bundles)."""
    client = _get_client()
    tools_mod = _get_tools()
    messages = [{"role": "user", "content": user_content}]
    bundles, lookups, creates = [], 0, 0
    while creates < 3:
        creates += 1
        kwargs = dict(
            model=config.MODEL,
            max_tokens=getattr(config, "MAX_TOKENS", 4096),
            system=system_blocks,
            messages=messages,
            tools=[PHARMACY_TOOL_SCHEMA],
        )
        response = _create_message(client, kwargs, actor="B")
        content = getattr(response, "content", []) or []
        tool_uses = [b for b in content if getattr(b, "type", "") == "tool_use"]
        if getattr(response, "stop_reason", None) == "tool_use" and tool_uses \
                and creates < 3:
            results = []
            for block in tool_uses:
                if block.name == "pharmacy_lookup" and lookups < 2:
                    pid = str((getattr(block, "input", {}) or {})
                              .get("patient_id") or patient_id)
                    bundle = tools_mod.pharmacy_lookup(pid)
                    n = len(bundle.get("entry", []))
                    lookups += 1
                    bundles.append(bundle)
                    _TOOL_TRACE.append({"agent": "B", "tool": "pharmacy_lookup",
                                        "patient_id": pid, "n_results": n})
                    events.emit(
                        "B", "tool",
                        "Acting as the nurse, we called up the pharmacy's "
                        "dispense records to see what was actually filled — "
                        "%d record(s) on file." % n,
                        patient_id=pid, n_results=n,
                    )
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": json.dumps(bundle)})
                else:
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": "Tool-call limit reached; finish "
                                               "the review with what you have.",
                                    "is_error": True})
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": results})
            continue
        text = _extract_text(response)
        try:
            return _parse_json(text), bundles
        except (ValueError, json.JSONDecodeError):
            if creates >= 3:
                break
            messages.append({"role": "assistant", "content": content or
                             [{"type": "text", "text": text or ""}]})
            messages.append({"role": "user",
                             "content": "Your previous reply was not valid "
                                        "JSON. Respond with ONLY the JSON "
                                        "array — no prose, no code fences."})
    return None, bundles


def _validate_external_evidence(challenges: list, patient_id: str):
    """verify_dispense every cited resource_id (B's grounding guarantee).

    Returns (challenges, dropped_ids). Invalid entries are stripped; a
    challenge whose evidence all fails keeps its text but loses the field.
    """
    tools_mod = _get_tools()
    dropped = []
    for ch in challenges:
        ev = ch.get("external_evidence")
        if ev is None:
            continue
        kept = []
        for item in ev if isinstance(ev, list) else []:
            rid = item.get("resource_id") if isinstance(item, dict) else None
            ok = False
            try:
                ok = bool(rid) and tools_mod.verify_dispense(rid, patient_id)
            except Exception:
                ok = False
            if ok:
                kept.append({
                    "resource_type": str(item.get("resource_type",
                                                  "MedicationDispense")),
                    "resource_id": rid,
                    "detail": str(item.get("detail", ""))[:300],
                })
            else:
                dropped.append(str(rid))
        if kept:
            ch["external_evidence"] = kept
        else:
            ch.pop("external_evidence", None)
    return challenges, dropped


def review(packet: list, rubric: list) -> list:
    """Agent B (nurse verifier): challenge the packet, or stand down with [].

    B has NO chart access — inputs are packet + rubric + (on demand) the
    pharmacy dispense feed via pharmacy_lookup, B's only tool. After the model
    responds, a code-level guard strips any challenge referencing material
    outside packet/rubric/retrieved-bundles, and verify_dispense validates
    every cited resource_id. Guard failures get one explained retry.
    """
    events.emit("B", "phase",
                "We're checking Agent A's evidence against the rubric. We "
                "never see the chart ourselves — only what A quoted, plus "
                "outside records like the pharmacy's if we need them.")
    system_blocks = _system_blocks("review", rubric_json=json.dumps(rubric, indent=2))
    patient_id = _LAST_PATIENT_ID or "patient1"
    user_content = (
        "PATIENT ID: " + patient_id + "\n\n"
        "EVIDENCE PACKET UNDER REVIEW:\n"
        + json.dumps(packet, indent=2)
        + "\n\nReview the packet against the rubric. JSON array only "
        "(return [] to stand down)."
    )
    raw, bundles = _review_round(system_blocks, user_content, patient_id)
    if raw is None:
        return _emit_review_result([])  # unusable output -> stand down
    bundle_blob = " ".join(json.dumps(b) for b in bundles)
    kept, violations, attempted = _guard_challenges(
        raw, packet, rubric, extra_blob=bundle_blob)
    kept, dropped = _validate_external_evidence(kept, patient_id)
    if (kept or not attempted) and not dropped:
        return _emit_review_result(kept)
    # Guard/dispense-check rejected something: retry once with the violations
    # explained, then give up (silently dropping anything still invalid).
    problems = violations[:8] + [
        "cited dispense resource '%s' does not exist in this patient's bundle"
        % rid for rid in dropped[:4]
    ]
    retry_content = (
        user_content
        + "\n\nYour previous challenges were REJECTED by a contract guard: "
        + "; ".join(problems)
        + ". Remember: you have NO chart access — every doc_id, quote, and "
        "date you mention must come from the packet, the rubric, or a "
        "dispense bundle you retrieved, and every external_evidence "
        "resource_id must exist in that bundle. Re-issue only grounded "
        "challenges, or return [] to stand down."
    )
    events.emit("B", "info",
                "Our first draft of the objections wasn't fully backed by "
                "evidence, so we're rewriting it.")
    raw2, bundles2 = _review_round(system_blocks, retry_content, patient_id)
    if raw2 is None:
        return _emit_review_result([])
    blob2 = " ".join(json.dumps(b) for b in bundles + bundles2)
    kept2, _, _ = _guard_challenges(raw2, packet, rubric, extra_blob=blob2)
    kept2, _ = _validate_external_evidence(kept2, patient_id)
    return _emit_review_result(kept2)


# ---------------------------------------------------------------------------
# REPAIR — Agent A, subsequent rounds
# ---------------------------------------------------------------------------

def _repair_queries(challenge: dict) -> list:
    """Derive up to 3 fresh search queries targeting what_would_satisfy."""
    wws = str(challenge.get("what_would_satisfy", "")).strip()
    queries = []
    if wws:
        queries.append(wws[:120])
    words = [w.strip(",.()'\"") for w in wws.split()]
    keywords = " ".join(w for w in words if len(w) > 4)[:80].strip()
    if keywords and keywords not in queries:
        queries.append(keywords)
    dates = _DATE_RE.findall(wws)
    if dates:
        q3 = ("symptom onset " + " ".join(dates)).strip()
        if q3 not in queries:
            queries.append(q3)
    return queries[:3] or ["clinical documentation resolving the challenge"]


def _concession_reasoning(challenge: dict) -> str:
    return (
        "Conceded after adversarial review: the chart could not satisfy this "
        "challenge (%s). Documentation that would settle it: %s"
        % (
            str(challenge.get("challenge_reason", "")).strip()[:200],
            str(challenge.get("what_would_satisfy", "")).strip(),
        )
    )


def repair(packet: list, challenges: list, corpus_dir: str) -> list:
    """Agent A patches the packet in response to Agent B's challenges.

    Each challenge's what_would_satisfy is targeted with fresh chart_search
    attempts. If the chart cannot satisfy it, the challenged item's status is
    forced to "insufficient" with reasoning that states the concession AND the
    documentation that would settle it. Returns the full patched packet.
    """
    tools_mod = _get_tools()
    challenges = [c for c in (challenges or []) if isinstance(c, dict)]
    events.emit("A", "phase",
                "We're going back to the chart to answer Agent B's "
                "%d objection(s)." % len(challenges))

    new_evidence = {}
    for challenge in challenges:
        try:
            cid = int(challenge.get("criterion_id"))
        except (TypeError, ValueError):
            continue
        found = []
        for query in _repair_queries(challenge):
            try:
                results = tools_mod.chart_search(query, corpus_dir) or []
            except Exception:
                results = []
            for r in results:
                if not isinstance(r, dict):
                    continue
                doc_id, quote = r.get("doc_id"), r.get("quote")
                date = r.get("date", "")
                if not (isinstance(doc_id, str) and isinstance(quote, str)):
                    continue
                try:
                    if tools_mod.verify_quote(doc_id, quote, corpus_dir):
                        found.append({"doc_id": doc_id, "quote": quote, "date": date})
                except Exception:
                    continue
            if found:
                break
        new_evidence[cid] = found
        if found:
            events.emit(
                "A", "search",
                "Question %s: we found new evidence in the chart that speaks "
                "to B's objection." % cid,
                criterion=cid, hits=len(found),
            )
        else:
            events.emit(
                "A", "search",
                "Question %s: we searched again — the chart simply doesn't "
                "contain what B asked for." % cid,
                criterion=cid, hits=0,
            )

    rubric = None
    try:
        rubric = tools_mod.rubric_lookup()
    except Exception:
        rubric = None
    if not rubric:
        rubric = [
            {"id": e.get("criterion_id"), "text": e.get("criterion_text", ""), "how": ""}
            for e in packet
        ]

    system_blocks = _system_blocks(
        "repair",
        rubric_json=json.dumps(rubric, indent=2),
        corpus_manifest=_corpus_manifest(corpus_dir),
    )
    user_payload = {
        "packet": packet,
        "challenges": challenges,
        "new_evidence": {str(k): v for k, v in new_evidence.items()},
    }
    user_content = (
        "Current packet, Agent B's challenges, and fresh verified search "
        "results per challenged criterion:\n"
        + json.dumps(user_payload, indent=2)
        + "\n\nReturn the FULL patched packet now. JSON array only."
    )
    raw = _call_claude(system_blocks, user_content, actor="A")
    if isinstance(raw, dict):
        raw = raw.get("packet") or []
    by_id = {}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                try:
                    by_id[int(entry.get("criterion_id"))] = entry
                except (TypeError, ValueError):
                    continue

    challenge_by_id = {}
    for challenge in challenges:
        try:
            challenge_by_id[int(challenge.get("criterion_id"))] = challenge
        except (TypeError, ValueError):
            continue

    patched = []
    for original in packet:
        try:
            cid = int(original.get("criterion_id"))
        except (TypeError, ValueError):
            patched.append(original)
            continue
        rubric_item = {
            "id": cid,
            "text": original.get("criterion_text", ""),
            "how": "",
        }
        allowed = {
            (ev.get("doc_id"), ev.get("quote"))
            for ev in (original.get("evidence") or [])
            if isinstance(ev, dict)
        }
        allowed.update((c["doc_id"], c["quote"]) for c in new_evidence.get(cid, []))
        entry = _normalize_entry(
            by_id.get(cid, original), rubric_item, corpus_dir, allowed
        )

        challenge = challenge_by_id.get(cid)
        if challenge is not None and not new_evidence.get(cid):
            # The chart could not satisfy the challenge: force the concession.
            events.emit(
                "A", "concede",
                "We concede question %s — the chart can't answer B's "
                "objection, so we're marking it 'not enough documentation'."
                % cid,
                criterion=cid,
            )
            entry["status"] = "insufficient"
            reasoning = entry.get("reasoning", "")
            wws = str(challenge.get("what_would_satisfy", "")).strip()
            if "conced" not in reasoning.lower():
                entry["reasoning"] = _concession_reasoning(challenge)
            elif wws and wws not in reasoning and "settle" not in reasoning.lower():
                entry["reasoning"] = (
                    reasoning + " Documentation that would settle it: " + wws
                )
        patched.append(entry)
    return patched


# ---------------------------------------------------------------------------
# NOTIFY — Agent B as communicator (post-verdict patient message)
# ---------------------------------------------------------------------------

def notify(run_state: dict) -> str:
    """Agent B drafts a patient-facing SMS about the verdict.

    Returns "" (skipped) for STABLE / in-progress states, or when no client
    is available (offline unit tests).
    """
    terminal = (run_state or {}).get("terminal_state")
    if terminal not in ("CONFIRMED_WORSENING", "INSUFFICIENT_EVIDENCE"):
        return ""
    if _client is None and not os.environ.get("ANTHROPIC_API_KEY"):
        return ""
    rubric_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "rubric.json"
    )
    with open(rubric_path, "r") as f:
        rubric = json.load(f)
    events.emit("B", "phase", "We're writing a plain-language text message to "
                              "the patient about what happens next…")
    summary = [
        {
            "criterion_id": e.get("criterion_id"),
            "status": e.get("status"),
            "reasoning": str(e.get("reasoning", ""))[:280],
        }
        for e in run_state.get("packet", [])
    ]
    system_blocks = _system_blocks("notify", rubric_json=json.dumps(rubric, indent=2))
    user_content = (
        "Verdict and packet summary:\n"
        + json.dumps({"terminal_state": terminal, "packet_summary": summary},
                     indent=2)
        + "\n\nDraft the SMS now. JSON object only."
    )
    try:
        raw = _call_claude(system_blocks, user_content, actor="B")
    except (ValueError, json.JSONDecodeError):
        return ""
    if isinstance(raw, dict):
        return str(raw.get("message", "")).strip()[:600]
    return ""
