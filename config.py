"""Configuration constants for cognition-project verification."""

import os
from pathlib import Path

# Path allowlist for changed files
PATH_ALLOWLIST = [
    "superset/",
    "tests/",
]

# Maximum lines of code (added + removed) allowed in a diff
MAX_DIFF_LOC = 100

# Bandit version (must match scanner)
BANDIT_VERSION = "1.7.9"

# Bandit command (must match scanner exactly)
BANDIT_CMD = "bandit -r superset/ -f json -q --exit-zero"

# Mapping of touched files to test subsets
TEST_MAPPING = {
    # Default if no specific mapping exists
    "default": "tests/unit_tests"
}

# Timeouts for subprocess execution (seconds)
BANDIT_TIMEOUT = 600  # 10 minutes
TEST_TIMEOUT = 1200  # 20 minutes

# Rule allowlist - only these rule IDs will be processed
RULE_ALLOWLIST = [
    "B608",  # SQL built by string construction
]

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
SESSION_TAG = "cognition-project"

# GitHub configuration
GITHUB_REPO = os.environ.get("GITHUB_REPO", "owner/repo")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# ACU configuration
MAX_ACU_PER_SESSION = 12

# Reconciler configuration
TICK_SECONDS = 30
MAX_CONCURRENT = 3
DAILY_ACU_CEILING = 100.0
WALL_CLOCK_MINUTES = 45
MAX_ATTEMPTS = 2

# Dry run flag - set via environment variable
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
