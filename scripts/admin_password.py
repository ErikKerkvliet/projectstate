"""Print a scrypt hash for ADMIN_PASSWORD_HASH. usage: python scripts/admin_password.py 'the-password'"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from projectstate.web.passwords import hash_password  # noqa: E402

print(hash_password(sys.argv[1]))
