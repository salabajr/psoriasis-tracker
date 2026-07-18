from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root (same dir as this file). Existing shell
# exports take precedence so CI / export ANTHROPIC_API_KEY=... still works.
load_dotenv(Path(__file__).resolve().parent / ".env")

MODEL = "claude-sonnet-4-6"   # spec-specified + ONLY place a model string lives.
                              # Confirm it resolves against the credit-linked
                              # account; if the event doc names another, use that.
TEMPERATURE = 0
MAX_ROUNDS = 3
CORPUS_DIR = "corpus/patient1"
CACHE_PREFIX = True           # mark system+rubric+corpus prefix with cache_control
