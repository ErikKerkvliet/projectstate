import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("BASE_URL", "http://127.0.0.1:8765")


@pytest.fixture
def db(tmp_path):
    from projectstate.db import Database, set_db

    d = Database(tmp_path / "t.db")
    set_db(d)
    yield d
    d.close()
    set_db(None)
