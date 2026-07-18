"""Acceptance tests for tools.py. Run: .venv/bin/python test_tools.py

All tests must pass offline (no ANTHROPIC_API_KEY) via the fallback path.
"""

import sys

from tools import chart_search, rubric_lookup, verify_quote

CORPUS = "fixtures/corpus_tiny"

passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}  {detail}")


# 1. verify_quote exactness
check("verify_quote verbatim hit",
      verify_quote("visit3_derm_note", "PASI 14.2, BSA 15%, IGA 3", CORPUS) is True)
check("verify_quote near-miss (missing comma) is False",
      verify_quote("visit3_derm_note", "PASI 14.2 BSA 15%", CORPUS) is False)
check("verify_quote unknown doc_id is False",
      verify_quote("no_such_note", "PASI 14.2", CORPUS) is False)
check("verify_quote case-sensitive (no normalization)",
      verify_quote("visit3_derm_note", "pasi 14.2, bsa 15%, iga 3", CORPUS) is False)
check("verify_quote NBSP normalized to space",
      verify_quote("visit3_derm_note", "PASI 14.2, BSA 15%, IGA 3", CORPUS) is True)

# 2. chart_search basic ranked results with verbatim quotes
results = chart_search("scalp plaque", CORPUS)
check("chart_search('scalp plaque') non-empty", len(results) > 0, repr(results))
keys_ok = all(set(r) == {"doc_id", "quote", "date", "score"} for r in results)
check("results have exactly keys doc_id/quote/date/score", keys_ok, repr(results))
check("results ranked by score desc",
      all(results[i]["score"] >= results[i + 1]["score"] for i in range(len(results) - 1)))
check("every quote verbatim (verify_quote passes)",
      all(verify_quote(r["doc_id"], r["quote"], CORPUS) for r in results), repr(results))
check("dates are YYYY-MM-DD",
      all(isinstance(r["date"], str) and len(r["date"]) == 10 for r in results),
      repr(results))
check("at most 5 results", len(results) <= 5)

# 3. Reformulation-miss demo:
#    "nail involvement" must NOT surface the nursing note (or return nothing);
#    "onycholysis" must hit visit3_nursing_note.
miss = chart_search("nail involvement", CORPUS)
miss_ok = all(r["doc_id"] != "visit3_nursing_note" for r in miss)
check("'nail involvement' returns nothing OR omits visit3_nursing_note",
      miss_ok, repr(miss))
print(f"      (actual behavior: 'nail involvement' -> {miss!r})")

hit = chart_search("onycholysis", CORPUS)
check("'onycholysis' hits visit3_nursing_note",
      any(r["doc_id"] == "visit3_nursing_note" for r in hit), repr(hit))
check("'onycholysis' quotes verbatim",
      all(verify_quote(r["doc_id"], r["quote"], CORPUS) for r in hit), repr(hit))
if miss:
    nursing_hit_score = max(r["score"] for r in hit if r["doc_id"] == "visit3_nursing_note")
    nursing_miss_scores = [r["score"] for r in miss if r["doc_id"] == "visit3_nursing_note"]
    if nursing_miss_scores:
        check("'onycholysis' scores nursing note strictly higher than 'nail involvement'",
              nursing_hit_score > max(nursing_miss_scores))

# 4. rubric_lookup returns the rubric array
rubric = rubric_lookup()
check("rubric_lookup returns non-empty list of dicts",
      isinstance(rubric, list) and len(rubric) == 5 and all(isinstance(x, dict) for x in rubric))
check("rubric items carry id/text/how",
      all({"id", "text", "how"} <= set(x) for x in rubric))

# 5. Zero-candidate query returns [] (and, offline, makes no API call by design)
check("gibberish query returns []", chart_search("zzqx floober", CORPUS) == [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
