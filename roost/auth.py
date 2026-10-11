"""Password setup and persistent, revocable owner sessions."""

import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

SESSION_SECONDS = 30 * 24 * 60 * 60
SCRYPT_N = 2**14


def make_password_hash(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Use a password of at least 12 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=8, p=1)
    return f"scrypt:{SCRYPT_N}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, salt, expected = encoded.strip().split(":")
        if algorithm != "scrypt" or int(n) != SCRYPT_N:
            return False
        result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=SCRYPT_N, r=8, p=1)
        return hmac.compare_digest(result, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def set_password(path: Path) -> None:
    first = getpass.getpass("New Roost password: ")
    second = getpass.getpass("Confirm Roost password: ")
    if first != second:
        raise ValueError("Passwords do not match.")
    encoded = make_password_hash(first)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    print("Roost password saved.")


class Sessions:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires INTEGER NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS sessions_expires ON sessions(expires)")
        self.db.commit()
        os.chmod(path, 0o600)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        self.db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
        self.db.execute("INSERT INTO sessions VALUES (?, ?)", (self._hash(token), now + SESSION_SECONDS))
        self.db.commit()
        return token

    def valid(self, token: str) -> bool:
        if not token or len(token) > 128:
            return False
        row = self.db.execute("SELECT expires FROM sessions WHERE token_hash = ?", (self._hash(token),)).fetchone()
        return bool(row and row[0] > int(time.time()))

    def revoke(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash(token),))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
