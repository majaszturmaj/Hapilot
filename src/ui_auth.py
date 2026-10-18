"""Optional password login for the Chainlit UI.

Login is enabled when ``HAPILOT_UI_USERS_FILE`` points to an existing file with lines
``username:scrypt$<n>$<r>$<p>$<salt-hex>$<hash-hex>`` (create entries with
``scripts/hapilotctl.py add-user NAME``). Chainlit also needs ``CHAINLIT_AUTH_SECRET``; the launcher
generates one under the data directory.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Optional

_N, _R, _P = 2 ** 14, 8, 1


def users_file() -> Optional[Path]:
    value = os.environ.get("HAPILOT_UI_USERS_FILE", "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_file() else None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r),
                                p=int(p), dklen=len(bytes.fromhex(hash_hex)))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def check_password(username: str, password: str, path: Optional[Path] = None) -> bool:
    path = path or users_file()
    if path is None:
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, encoded = line.strip().partition(":")
        if name and name == username:
            return verify_password(password, encoded)
    return False
