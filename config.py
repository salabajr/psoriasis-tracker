from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root (same dir as this file). Existing shell
# exports take precedence so CI / export ANTHROPIC_API_KEY=... still works.
load_dotenv(Path(__file__).resolve().parent / ".env")

MODEL = "claude-fable-5"      # demo model (spec default was claude-sonnet-4-6).
                              # ONLY place a model string lives.
FALLBACK_MODEL = "claude-opus-4-8"  # server-side refusal fallback for Fable 5
TEMPERATURE = 0               # ignored on Fable 5 (parameter rejected there)

# Claude Fable 5 API differences (also applies to claude-mythos-*):
# temperature is rejected (400), thinking is always on (give max_tokens
# headroom so thinking doesn't eat the JSON), refusal fallbacks ride on the
# beta endpoint.
IS_FABLE = MODEL.startswith(("claude-fable", "claude-mythos"))
MAX_TOKENS = 16000 if IS_FABLE else 4096
MAX_ROUNDS = 3
CORPUS_DIR = "corpus/patient1"
CACHE_PREFIX = True           # mark system+rubric+corpus prefix with cache_control
