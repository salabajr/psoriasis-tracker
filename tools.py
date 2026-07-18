"""Chart tools: chart_search, rubric_lookup, verify_quote.

Frozen signatures per contracts.md. No API calls anywhere except
chart_search stage 2 (a single Claude call, which degrades gracefully to a
deterministic pure-Python ranking when ANTHROPIC_API_KEY is unset or the
call fails).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from config import MODEL, TEMPERATURE, CACHE_PREFIX

MAX_RESULTS = 5

# Prefilter: a doc is a candidate only if strictly more than half of the
# query's content tokens appear in it. This is deliberate: "nail involvement"
# (1/2 tokens in the nursing note) must NOT match, while "onycholysis"
# (1/1) must. This is what makes the reformulation-miss demo work.
_CANDIDATE_THRESHOLD = 0.5  # strict > comparison

_STOPWORDS = {
    "a", "an", "the", "of", "on", "in", "and", "or", "for", "to", "with",
    "is", "are", "was", "were", "at", "by", "as", "be", "has", "have", "had",
    "this", "that", "it", "its", "no", "not",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DATE_HEADER_RE = re.compile(r"\*\*Date:\*\*\s*(\d{4}-\d{2}-\d{2})")
_ANY_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------
# Corpus loading and tokenization (pure Python)
# ---------------------------------------------------------------------------

def _load_corpus(corpus_dir: str) -> dict[str, dict]:
    """Return {doc_id: {"text": str, "date": str|None}} for every .md file."""
    corpus: dict[str, dict] = {}
    base = Path(corpus_dir)
    if not base.is_dir():
        return corpus
    for path in sorted(base.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        m = _DATE_HEADER_RE.search(text)
        if not m:
            m = _ANY_DATE_RE.search(text)
        corpus[path.stem] = {"text": text, "date": m.group(1) if m and m.lastindex else (m.group(0) if m else None)}
    return corpus


def _stem(token: str) -> str:
    """Very light plural stemming: strip a trailing 's' on longer tokens.

    "plaques" -> "plaque", but "fingernails" -> "fingernail" != "nail",
    so partial-word matches never happen (tokens are whole words only).
    """
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokenize(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    return [_stem(t) for t in tokens if t not in _STOPWORDS]


def _prefilter(query: str, corpus: dict[str, dict]) -> list[tuple[str, float]]:
    """Stage 1: keyword overlap. Returns [(doc_id, score)] sorted desc.

    score = fraction of query content-tokens present in the doc.
    Candidates require score strictly greater than _CANDIDATE_THRESHOLD.
    """
    qtokens = set(_tokenize(query))
    if not qtokens:
        return []
    scored = []
    for doc_id, doc in corpus.items():
        dtokens = set(_tokenize(doc["text"]))
        overlap = len(qtokens & dtokens) / len(qtokens)
        if overlap > _CANDIDATE_THRESHOLD:
            scored.append((doc_id, overlap))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_quote(doc_id: str, quote: str, corpus_dir: str) -> bool:
    """Exact substring check. Only Unicode NBSP is normalized to a space.

    No case folding, no whitespace-run collapsing — exact means exact.
    Returns False for unknown doc_id or empty quote.
    """
    if not quote:
        return False
    path = Path(corpus_dir) / f"{doc_id}.md"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8").replace(" ", " ")
    return quote.replace(" ", " ") in text


def rubric_lookup() -> list[dict]:
    """Return the rubric.json array (repo root, next to this file)."""
    rubric_path = Path(__file__).resolve().parent / "rubric.json"
    with open(rubric_path, encoding="utf-8") as f:
        return json.load(f)


def chart_search(query: str, corpus_dir: str) -> list[dict]:
    """Two-stage chart search.

    Stage 1: pure-Python keyword prefilter (no API). Zero candidates -> [].
    Stage 2: one Claude call ranks candidates and extracts verbatim quotes.
    Every returned quote is re-validated with verify_quote; non-verbatim
    quotes are dropped. If the API key is missing or the call fails, a
    deterministic pure-Python fallback ranking is used instead.
    """
    corpus = _load_corpus(corpus_dir)
    candidates = _prefilter(query, corpus)
    if not candidates:
        return []

    results: list[dict] | None = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            results = _rank_with_claude(query, candidates, corpus, corpus_dir)
        except Exception as exc:  # any API/parse failure -> offline fallback
            print(f"[tools] chart_search: Claude ranking failed ({exc!r}); "
                  "falling back to keyword ranking", file=sys.stderr)
            results = None
    else:
        print("[tools] chart_search: ANTHROPIC_API_KEY unset; using offline "
              "keyword ranking", file=sys.stderr)

    if results is None:
        results = _rank_offline(query, candidates, corpus, corpus_dir)
    return results[:MAX_RESULTS]


# ---------------------------------------------------------------------------
# Stage 2a: Claude ranking (the ONLY API call in this module)
# ---------------------------------------------------------------------------

_SEARCH_SYSTEM_PROMPT = (
    "You are a clinical chart search engine. You are given a set of chart "
    "documents and a query. Rank the documents by relevance to the query and "
    "extract, for each relevant document, ONE short quote that is a VERBATIM, "
    "character-for-character substring of that document (copy it exactly, "
    "including punctuation and capitalization; never paraphrase, never merge "
    "text across line breaks).\n\n"
    "Respond with ONLY a JSON array, no prose, of at most 5 objects:\n"
    '[{"doc_id": "<doc id>", "quote": "<verbatim substring>", '
    '"date": "YYYY-MM-DD", "score": <float 0..1, higher = more relevant>}]\n'
    "Omit documents that are not relevant. If nothing is relevant, return []."
)


def _corpus_block(candidates: list[tuple[str, float]], corpus: dict[str, dict]) -> str:
    parts = []
    for doc_id, _ in candidates:
        doc = corpus[doc_id]
        parts.append(
            f'<document doc_id="{doc_id}" date="{doc["date"] or "unknown"}">\n'
            f'{doc["text"]}\n</document>'
        )
    return "CANDIDATE DOCUMENTS:\n\n" + "\n\n".join(parts)


def _rank_with_claude(
    query: str,
    candidates: list[tuple[str, float]],
    corpus: dict[str, dict],
    corpus_dir: str,
) -> list[dict]:
    import anthropic  # imported lazily; only needed on the live path

    # Stable cached prefix: system prompt + full candidate corpus block, with
    # cache_control on the last stable block. The query is the variable tail
    # (a plain user message) and is never interleaved into the cached prefix.
    corpus_block: dict = {"type": "text", "text": _corpus_block(candidates, corpus)}
    if CACHE_PREFIX:
        corpus_block["cache_control"] = {"type": "ephemeral"}
    system = [{"type": "text", "text": _SEARCH_SYSTEM_PROMPT}, corpus_block]

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        temperature=TEMPERATURE,
        system=system,
        messages=[{"role": "user", "content": f"Query: {query}\n\nReturn the JSON array now."}],
    )

    text = "".join(b.text for b in response.content if b.type == "text")
    raw = _extract_json_array(text)

    results = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        doc_id = entry.get("doc_id")
        quote = entry.get("quote")
        if doc_id not in corpus or not isinstance(quote, str):
            continue
        # Post-validate: drop anything that is not a verbatim substring.
        if not verify_quote(doc_id, quote, corpus_dir):
            print(f"[tools] chart_search: dropped non-verbatim quote for "
                  f"{doc_id!r}", file=sys.stderr)
            continue
        try:
            score = float(entry.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        results.append({
            "doc_id": doc_id,
            "quote": quote,
            # Trust our own header-parsed date over the model's.
            "date": corpus[doc_id]["date"] or entry.get("date"),
            "score": score,
        })
    results.sort(key=lambda r: -r["score"])
    return results


def _extract_json_array(text: str) -> list:
    start = text.find("[")
    if start == -1:
        raise ValueError("no JSON array in model response")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, list):
        raise ValueError("model response was not a JSON array")
    return value


# ---------------------------------------------------------------------------
# Stage 2b: deterministic offline fallback (no API)
# ---------------------------------------------------------------------------

_FRAGMENT_SPLIT_RE = re.compile(r"(?<=[.;:!?])\s+|\n+")


def _best_fragment(query: str, text: str) -> str | None:
    """Best-matching sentence/line fragment; always a verbatim substring."""
    qtokens = set(_tokenize(query))
    best, best_hits = None, 0
    for frag in _FRAGMENT_SPLIT_RE.split(text):
        frag = frag.strip().lstrip("#-*> ").strip()
        if len(frag) < 3:
            continue
        hits = len(qtokens & set(_tokenize(frag)))
        if hits > best_hits:
            best, best_hits = frag, hits
    return best


def _rank_offline(
    query: str,
    candidates: list[tuple[str, float]],
    corpus: dict[str, dict],
    corpus_dir: str,
) -> list[dict]:
    results = []
    for doc_id, score in candidates:
        quote = _best_fragment(query, corpus[doc_id]["text"])
        if quote is None or not verify_quote(doc_id, quote, corpus_dir):
            continue
        results.append({
            "doc_id": doc_id,
            "quote": quote,
            "date": corpus[doc_id]["date"],
            "score": round(score, 4),
        })
    return results
