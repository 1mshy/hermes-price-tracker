"""Test env: isolated file-backed SQLite, no playbook writes, no Chromium.

Must run before any pricewatch import — settings is a module-level singleton
read from the environment at import time.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="pricewatch-tests-")
os.environ.setdefault("PW_DB_URL", f"sqlite:///{_TMP}/pricewatch_test.db")
os.environ.setdefault("PW_PLAYBOOK_PATH", "off")
os.environ.setdefault("PW_BROWSER_ENABLED", "false")
os.environ.setdefault("PW_CHECK_CRON", "*/30 * * * *")
