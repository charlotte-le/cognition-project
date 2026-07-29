"""Configuration constants for cognition-project verification.

Every operational limit here is overridable from the environment. The defaults
are the safe-by-inspection values; .env is what the operator actually intends.
A knob that silently ignores .env is worse than no knob at all.
"""

import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_list(name: str, default: list) -> list:
    raw = os.environ.get(name)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


# Path allowlist for changed files
PATH_ALLOWLIST = _env_list("PATH_ALLOWLIST", [
    "superset/",
    "tests/",
])

# Maximum lines of code (added + removed) allowed in a diff
MAX_DIFF_LOC = _env_int("MAX_DIFF_LOC", 100)

# Bandit version (must match scanner)
BANDIT_VERSION = "1.7.9"

# Bandit command (must match scanner exactly)
BANDIT_CMD = "bandit -r superset/ -f json -q --exit-zero"

# Whether gate 4 runs the target repo's test suite.
#
# Off by default. Running it requires the verifier host to carry the target
# repo's dependencies - superset's tests/conftest.py imports flask - and a gate
# that cannot run returns "infra" on every tick, which is strictly worse than a
# gate that honestly reports it did not run. The oracle gate needs none of that:
# Bandit is static analysis, so the proof that the finding is gone stands on its
# own.
#
# Turn this on where the environment can support it. In a real deployment the
# better answer is to read the PR's CI check-run conclusion from GitHub rather
# than rebuilding the target repo's test environment here.
RUN_TESTS_GATE = os.environ.get("RUN_TESTS_GATE", "false").lower() == "true"

# Mapping of rule IDs to test subsets
# Keys can be rule IDs (e.g., "B608") or file patterns
# Values are pytest paths relative to the target repo root
# Used by verifier.py to select appropriate tests for each security finding
TEST_MAPPING = {
    # Default if no specific mapping exists
    "default": "tests/unit_tests",
    # B608: SQL built by string construction - use a small, fast test subset.
    # This avoids running the entire 10-20 minute test suite on every PR.
    # Must be a path that exists in the target repo: a missing path makes pytest
    # exit 4, which the verifier (correctly) reads as infra, so the gate never
    # returns a real answer.
    "B608": "tests/unit_tests/utils"
}

# Checkout of the repository under review, where Bandit and the tests run.
# This is the target repo (must contain superset/ and tests/), NOT the
# cognition-project repo the reconciler itself lives in.
TARGET_REPO_PATH = Path(os.environ.get("TARGET_REPO_PATH", "/repo"))

# Timeouts for subprocess execution (seconds)
BANDIT_TIMEOUT = _env_int("BANDIT_TIMEOUT", 600)  # 10 minutes
TEST_TIMEOUT = _env_int("TEST_TIMEOUT", 120)  # 2 minutes (reduced for scoped tests)
CHECKOUT_TIMEOUT = _env_int("CHECKOUT_TIMEOUT", 300)  # 5 minutes

# Rule allowlist - only these rule IDs will be processed
RULE_ALLOWLIST = _env_list("RULE_ALLOWLIST", [
    "B608",  # SQL built by string construction
])

# Database path (use /data in Docker, local data directory otherwise)
if os.environ.get("DB_PATH"):
    DB_PATH = os.environ["DB_PATH"]
elif os.path.exists("/data"):
    DB_PATH = "/data/cognition.db"
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), "data", "cognition.db")

# Devin API configuration
DEVIN_API_BASE = os.environ.get("DEVIN_API_BASE", "https://api.devin.ai")
DEVIN_API_KEY = os.environ.get("DEVIN_API_KEY", "")
DEVIN_ORG_ID = os.environ.get("DEVIN_ORG_ID", "")

# Session tag for correlation
SESSION_TAG = os.environ.get("SESSION_TAG", "cognition-project")

# GitHub configuration
GITHUB_REPO = os.environ.get("GITHUB_REPO", "owner/repo")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# ACU configuration
MAX_ACU_PER_SESSION = _env_int("MAX_ACU_PER_SESSION", 12)

# Reconciler configuration
TICK_SECONDS = _env_int("TICK_SECONDS", 30)
MAX_CONCURRENT = _env_int("MAX_CONCURRENT", 3)
DAILY_ACU_CEILING = _env_float("DAILY_ACU_CEILING", 100.0)
WALL_CLOCK_MINUTES = _env_int("WALL_CLOCK_MINUTES", 45)
MAX_ATTEMPTS = _env_int("MAX_ATTEMPTS", 2)

# Dry run flag - set via environment variable
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
