from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root (same dir as this file). Existing shell
# exports take precedence so CI / export ANTHROPIC_API_KEY=... still works.
load_dotenv(Path(__file__).resolve().parent / ".env")

MODEL = "claude-opus-4-8"     # demo model — optimized for inference speed:
                              # no always-on thinking (unlike claude-fable-5),
                              # Opus-tier quality, ~seconds per call.
                              # ONLY place a model string lives.
FALLBACK_MODEL = "claude-opus-4-8"  # refusal fallback target when on Fable 5
TEMPERATURE = 0               # sent only to models that still accept it

# Modern-surface models (Fable 5 / Mythos / Opus 4.7+ / Sonnet 5) reject
# sampling parameters — omit temperature there.
IS_FABLE = MODEL.startswith(("claude-fable", "claude-mythos"))
NO_TEMPERATURE = IS_FABLE or MODEL.startswith(
    ("claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5"))

# Fast mode (research preview, Opus 4.8): flip on when the org is granted
# access — currently rate-limited to 0 fast-mode tokens on this account.
FAST_MODE = False

if IS_FABLE:
    # Fable 5: server-side refusal fallback rides on the beta endpoint.
    USE_BETA = True
    BETA_KWARGS = {"betas": ["server-side-fallback-2026-06-01"],
                   "fallbacks": [{"model": FALLBACK_MODEL}]}
elif FAST_MODE and MODEL.startswith("claude-opus-4-8"):
    USE_BETA = True
    BETA_KWARGS = {"betas": ["fast-mode-2026-02-01"], "speed": "fast"}
else:
    USE_BETA = False
    BETA_KWARGS = {}

MAX_TOKENS = 16000 if IS_FABLE else 8000  # Fable needs thinking headroom
MAX_ROUNDS = 3
CORPUS_DIR = "corpus/patient1"
CACHE_PREFIX = True           # mark system+rubric+corpus prefix with cache_control
