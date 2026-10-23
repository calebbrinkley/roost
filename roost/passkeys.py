"""Passkeys (WebAuthn) for Roost's single owner: registration, sign-in and storage.

Only what Roost needs. The owner adds passkeys while signed in, so no attestation is requested
or checked. Every passkey must be discoverable and every use must be user-verified (Face ID,
Touch ID or the device's PIN). Signatures are checked with the `cryptography` package.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

CHALLENGE_SECONDS = 300
PENDING_CHALLENGES = 32
MAX_PASSKEYS = 20
TIMEOUT_MS = 120000
# COSE algorithm identifiers, in the order Roost prefers them.
ES256, EDDSA, RS256 = -7, -8, -257
ALGORITHMS = (ES256, EDDSA, RS256)
FLAG_USER_PRESENT, FLAG_USER_VERIFIED, FLAG_ATTESTED = 0x01, 0x04, 0x40
BASE64URL = re.compile(r"^[A-Za-z0-9_-]*$")


class PasskeyError(Exception):
    """A rejected registration or sign-in. The message is safe to show to the owner."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(value, limit: int = 8192) -> bytes:
    if not isinstance(value, str) or len(value) > limit or not BASE64URL.fullmatch(value):
        raise PasskeyError("The passkey response was malformed.")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError:
        raise PasskeyError("The passkey response was malformed.") from None


def cbor_item(data: bytes, position: int = 0, depth: int = 0):
    """Decode one CBOR item; return (value, next position). Only the forms authenticators use."""
    malformed = PasskeyError("The passkey response was malformed.")
    if depth > 8 or position >= len(data):
        raise malformed
    initial = data[position]
    major, info = initial >> 5, initial & 0x1F
    position += 1
    if info < 24:
        argument = info
    elif info <= 27:
        size = 1 << (info - 24)
        if position + size > len(data):
            raise malformed
        argument = int.from_bytes(data[position:position + size], "big")
        position += size
    else:
        raise malformed  # indefinite lengths and reserved values
    if major == 0:
        return argument, position
    if major == 1:
        return -1 - argument, position
    if major in (2, 3):
        if position + argument > len(data):
            raise malformed
        chunk = data[position:position + argument]
        position += argument
        if major == 2:
            return bytes(chunk), position
        try:
            return chunk.decode("utf-8"), position
        except UnicodeDecodeError:
            raise malformed from None
    if major == 4:
        if argument > 64:
            raise malformed
        items = []
        for _ in range(argument):
            item, position = cbor_item(data, position, depth + 1)
            items.append(item)
        return items, position
    if major == 5:
        if argument > 64:
            raise malformed
        result = {}
        for _ in range(argument):
            key, position = cbor_item(data, position, depth + 1)
            if not isinstance(key, (int, str)) or key in result:
                raise malformed
            result[key], position = cbor_item(data, position, depth + 1)
        return result, position
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], position
    raise malformed


def public_key_from_cose(key) -> tuple[int, bytes]:
    """The algorithm and SubjectPublicKeyInfo DER for a COSE public key Roost accepts."""
    unsupported = PasskeyError("This passkey uses a kind of key Roost doesn't support.")
    if not isinstance(key, dict):
        raise unsupported
    kty, algorithm = key.get(1), key.get(3)
    try:
        if algorithm == ES256 and kty == 2 and key.get(-1) == 1:
            x, y = key.get(-2), key.get(-3)
            if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == len(y) == 32):
                raise unsupported
            # Raises ValueError for a point that is not on the curve.
            public = ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
        elif algorithm == EDDSA and kty == 1 and key.get(-1) == 6:
            x = key.get(-2)
            if not (isinstance(x, bytes) and len(x) == 32):
                raise unsupported
            public = ed25519.Ed25519PublicKey.from_public_bytes(x)
        elif algorithm == RS256 and kty == 3:
            n, e = key.get(-1), key.get(-2)
            if not (isinstance(n, bytes) and isinstance(e, bytes) and 256 <= len(n) <= 512 and 0 < len(e) <= 8):
                raise unsupported
            public = rsa.RSAPublicNumbers(int.from_bytes(e, "big"), int.from_bytes(n, "big")).public_key()
        else:
            raise unsupported
    except ValueError:
        raise unsupported from None
    return algorithm, public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def verify_signature(algorithm: int, public_key: bytes, signature: bytes, signed: bytes) -> None:
    public = serialization.load_der_public_key(public_key)
    try:
        if algorithm == ES256:
            public.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        elif algorithm == EDDSA:
            public.verify(signature, signed)
        elif algorithm == RS256:
            public.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise PasskeyError("This passkey uses a kind of key Roost doesn't support.")
    except InvalidSignature:
        raise PasskeyError("The passkey's signature didn't check out.") from None


class Passkeys:
    """Registered passkeys and pending challenges for one origin. Shares the sessions database."""

    def __init__(self, db: sqlite3.Connection, origin: str, rp_id: str):
        self.db, self.origin, self.rp_id = db, origin, rp_id
        self.rp_id_hash = hashlib.sha256(rp_id.encode()).digest()
        self.challenges: dict[str, tuple[str, float, str | None]] = {}
        db.execute("CREATE TABLE IF NOT EXISTS passkeys (id TEXT PRIMARY KEY, public_key BLOB NOT NULL, "
                   "algorithm INTEGER NOT NULL, sign_count INTEGER NOT NULL, name TEXT NOT NULL, "
                   "created INTEGER NOT NULL, last_used INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS passkey_owner (handle BLOB NOT NULL)")
        if db.execute("SELECT COUNT(*) FROM passkey_owner").fetchone()[0] == 0:
            db.execute("INSERT INTO passkey_owner VALUES (?)", (secrets.token_bytes(32),))
        db.commit()

    def owner_handle(self) -> bytes:
        return self.db.execute("SELECT handle FROM passkey_owner").fetchone()[0]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM passkeys").fetchone()[0]

    def list(self) -> list[dict]:
        rows = self.db.execute("SELECT id, name, created, last_used FROM passkeys ORDER BY created").fetchall()
        return [{"id": row[0], "name": row[1], "created": row[2], "last_used": row[3]} for row in rows]

    def remove(self, credential_id: str) -> bool:
        removed = self.db.execute("DELETE FROM passkeys WHERE id = ?", (credential_id,)).rowcount
        self.db.commit()
        return bool(removed)

    def _challenge(self, purpose: str, bound: str | None = None) -> str:
        now = time.monotonic()
        for value, (_, expires, _) in list(self.challenges.items()):
            if expires < now:
                del self.challenges[value]
        # Unauthenticated callers can ask for sign-in challenges: keep only the newest few.
        while len(self.challenges) >= PENDING_CHALLENGES:
            del self.challenges[next(iter(self.challenges))]
        value = b64url(secrets.token_bytes(32))
        self.challenges[value] = (purpose, now + CHALLENGE_SECONDS, bound)
        return value

    def _client_data(self, raw: bytes, kind: str, purpose: str, bound: str | None = None) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise PasskeyError("The passkey response was malformed.") from None
        if not isinstance(data, dict) or data.get("type") != kind:
            raise PasskeyError("The passkey response was malformed.")
        # Single use: a challenge is spent by the first response that names it, valid or not.
        pending = self.challenges.pop(data.get("challenge") if isinstance(data.get("challenge"), str) else "", None)
        if not pending or pending[0] != purpose or pending[1] < time.monotonic() or pending[2] != bound:
            raise PasskeyError("That passkey request expired. Try again.")
        if data.get("origin") != self.origin or data.get("crossOrigin") is True:
            raise PasskeyError("That passkey response came from another site.")

    def _authenticator(self, data: bytes, *, attested: bool) -> dict:
        if len(data) < 37:
            raise PasskeyError("The passkey response was malformed.")
        if not hmac.compare_digest(data[:32], self.rp_id_hash):
            raise PasskeyError("That passkey belongs to another site.")
        flags = data[32]
        if not flags & FLAG_USER_PRESENT or not flags & FLAG_USER_VERIFIED:
            raise PasskeyError("Face ID, Touch ID or your device PIN has to confirm it's you.")
        result = {"sign_count": int.from_bytes(data[33:37], "big")}
        if attested:
            if not flags & FLAG_ATTESTED or len(data) < 55:
                raise PasskeyError("The passkey response was malformed.")
            position = 37 + 16  # skip the authenticator model identifier (AAGUID)
            length = int.from_bytes(data[position:position + 2], "big")
            position += 2
            if not 16 <= length <= 1023 or position + length > len(data):
                raise PasskeyError("The passkey response was malformed.")
            result["credential_id"] = data[position:position + length]
            key, _ = cbor_item(data, position + length)  # extensions may follow the key
            result["algorithm"], result["public_key"] = public_key_from_cose(key)
        return result

    def registration_options(self, session: str) -> dict:
        return {
            "challenge": self._challenge("register", session),
            "rp": {"id": self.rp_id, "name": "Roost"},
            "user": {"id": b64url(self.owner_handle()), "name": "Roost owner", "displayName": "Roost owner"},
            "pubKeyCredParams": [{"type": "public-key", "alg": algorithm} for algorithm in ALGORITHMS],
            "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True, "userVerification": "required"},
            "attestation": "none",
            "excludeCredentials": [{"type": "public-key", "id": row["id"]} for row in self.list()],
            "timeout": TIMEOUT_MS,
        }

    def register(self, response: dict, session: str, name: str) -> dict:
        if self.count() >= MAX_PASSKEYS:
            raise PasskeyError(f"Roost keeps at most {MAX_PASSKEYS} passkeys. Remove one first.")
        self._client_data(unb64url(response.get("clientDataJSON")), "webauthn.create", "register", session)
        attestation, _ = cbor_item(unb64url(response.get("attestationObject"), 65536))
        if not isinstance(attestation, dict) or not isinstance(attestation.get("authData"), bytes):
            raise PasskeyError("The passkey response was malformed.")
        authenticator = self._authenticator(attestation["authData"], attested=True)
        credential_id = b64url(authenticator["credential_id"])
        if response.get("id") != credential_id:
            raise PasskeyError("The passkey response was malformed.")
        try:
            self.db.execute("INSERT INTO passkeys VALUES (?, ?, ?, ?, ?, ?, NULL)",
                            (credential_id, authenticator["public_key"], authenticator["algorithm"],
                             authenticator["sign_count"], name, int(time.time())))
        except sqlite3.IntegrityError:
            raise PasskeyError("That passkey is already added.") from None
        self.db.commit()
        return {"id": credential_id, "name": name}

    def authentication_options(self) -> dict:
        # No allow-list: the device offers the owner's discoverable passkeys for this site.
        return {"challenge": self._challenge("login"), "rpId": self.rp_id, "userVerification": "required",
                "allowCredentials": [], "timeout": TIMEOUT_MS}

    def authenticate(self, response: dict) -> None:
        credential_id = response.get("id")
        row = self.db.execute("SELECT public_key, algorithm, sign_count FROM passkeys WHERE id = ?",
                              (credential_id,)).fetchone() if isinstance(credential_id, str) and len(credential_id) <= 1400 else None
        client_raw = unb64url(response.get("clientDataJSON"))
        self._client_data(client_raw, "webauthn.get", "login")
        if row is None:
            raise PasskeyError("That passkey isn't set up for Roost. Sign in with your password.")
        authenticator_raw = unb64url(response.get("authenticatorData"))
        authenticator = self._authenticator(authenticator_raw, attested=False)
        handle = response.get("userHandle")
        if handle is not None and not hmac.compare_digest(unb64url(handle), self.owner_handle()):
            raise PasskeyError("That passkey belongs to someone else.")
        public_key, algorithm, stored = row
        verify_signature(algorithm, public_key, unb64url(response.get("signature")),
                         authenticator_raw + hashlib.sha256(client_raw).digest())
        # Synced passkeys (iCloud Keychain) always report 0; a counter that goes backwards means a copy.
        count = authenticator["sign_count"]
        if (count or stored) and count <= stored:
            raise PasskeyError("That passkey looks copied. Remove it in Settings and add it again.")
        self.db.execute("UPDATE passkeys SET sign_count = ?, last_used = ? WHERE id = ?",
                        (count, int(time.time()), credential_id))
        self.db.commit()
