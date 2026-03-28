"""
Local Test Script (moved from test_local.py)
────────────────────────────────────────
Run this on your local machine BEFORE deploying to GitHub Actions.
Tests each module independently so you can catch issues early.

Usage:
  cd src
  python ../local_test.py

  Or test a specific module:
  python ../local_test.py --test telegram
  python ../local_test.py --test login
  python ../local_test.py --test data
  python ../local_test.py --test full
"""

import asyncio
import sys
import os
import argparse
from pathlib import Path
from dotenv import load_dotenv

# ── Load .env file ─────────────────────────────────────────────────────────
# Load from project root (one level up from src/)
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

# Add src/ to path so we can import our modules
sys.path.insert(0, str(Path(__file__).parent / "src"))

# (rest of file unchanged)

from dev_tools.local_test_script import main as _main_runner

if __name__ == "__main__":
    # Reuse the existing runner implementation in `test_local.py`
    # to avoid duplicating the large content here.
    success = asyncio.run(_main_runner())
    sys.exit(0 if success else 1)
