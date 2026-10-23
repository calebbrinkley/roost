"""Web Push: Roost's VAPID identity (RFC 8292), payload encryption (RFC 8291) and delivery (RFC 8030).

Each notification is encrypted for one browser's keys before it leaves the Roost server, so the push service
(Apple, Google, Mozilla or Microsoft) relays it without being able to read it.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RECORD_SIZE = 4096
MAX_PAYLOAD = 3000
MAX_SUBSCRIPTIONS = 20
TTL_SECONDS = 24 * 60 * 60
# Browsers' push services. Roost delivers only to these, never to an arbitrary address.
PUSH_HOSTS = ("push.apple.com", "fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def point(public: ec.EllipticCurvePublicKey) -> bytes:
    return public.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def allowed_endpoint(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https" and not parts.username and not parts.password and parts.port in (None, 443)
            and any(host == allowed or host.endswith("." + allowed) for allowed in PUSH_HOSTS))


def encrypt(payload: bytes, ua_public: bytes, auth_secret: bytes, *, salt: bytes | None = None,
            as_private: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """aes128gcm content coding for one browser subscription (RFC 8291 section 3, one record)."""
    salt = salt or secrets.token_bytes(16)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_public = point(as_private.public_key())
    shared = as_private.exchange(ec.ECDH(), ua_key)
    prk_key = _hmac(auth_secret, shared)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public + ciphertext


def vapid_authorization(key: ec.EllipticCurvePrivateKey, endpoint: str, subject: str, now: float | None = None) -> str:
    """The VAPID Authorization header value: a 12-hour ES256 JWT for the push service's origin."""
    parts = urlsplit(endpoint)
    header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64url(json.dumps({"aud": f"{parts.scheme}://{parts.netloc}", "exp": int((now or time.time()) + 12 * 3600),
                                "sub": subject}, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    signature = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={header}.{claims}.{signature}, k={b64url(point(key.public_key()))}"


def load_vapid_key(path: Path) -> ec.EllipticCurvePrivateKey:
    """Roost's push identity, created once and kept in its state directory."""
    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    except FileNotFoundError:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(pem)
        return key


class PushError(Exception):
    """A rejected subscription. The message is safe to show to the owner."""


class Push:
    """Push subscriptions (one per browser) and delivery to them. Shares the sessions database."""

    def __init__(self, db: sqlite3.Connection, key_path: Path, subject: str):
        self.db, self.subject = db, subject
        self.key = load_vapid_key(key_path)
        db.execute("CREATE TABLE IF NOT EXISTS push_subscriptions (id TEXT PRIMARY KEY, endpoint TEXT UNIQUE NOT NULL, "
                   "p256dh BLOB NOT NULL, auth BLOB NOT NULL, name TEXT NOT NULL, created INTEGER NOT NULL, "
                   "last_sent INTEGER)")
        db.commit()

    def public_key(self) -> str:
        return b64url(point(self.key.public_key()))

    def list(self) -> list[dict]:
        rows = self.db.execute("SELECT id, name, created, last_sent FROM push_subscriptions ORDER BY created").fetchall()
        return [{"id": row[0], "name": row[1], "created": row[2], "last_sent": row[3]} for row in rows]

    def subscribe(self, subscription: dict, name: str) -> dict:
        endpoint = subscription.get("endpoint")
        keys = subscription.get("keys") if isinstance(subscription.get("keys"), dict) else {}
        if not isinstance(endpoint, str) or len(endpoint) > 1024 or not allowed_endpoint(endpoint):
            raise PushError("This browser's push service isn't one Roost sends to.")
        try:
            p256dh, auth = unb64url(keys.get("p256dh", "")), unb64url(keys.get("auth", ""))
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
        except (ValueError, TypeError, AttributeError):
            raise PushError("The browser sent an unusable push subscription.") from None
        if len(auth) != 16:
            raise PushError("The browser sent an unusable push subscription.")
        if self.db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE endpoint != ?", (endpoint,)).fetchone()[0] >= MAX_SUBSCRIPTIONS:
            raise PushError(f"Roost sends to at most {MAX_SUBSCRIPTIONS} devices. Remove one first.")
        subscription_id = hashlib.sha256(endpoint.encode()).hexdigest()[:24]
        # A browser that renews its subscription keeps its place (and name) in the list.
        self.db.execute("INSERT INTO push_subscriptions VALUES (?, ?, ?, ?, ?, ?, NULL) ON CONFLICT(endpoint) DO UPDATE "
                        "SET p256dh = excluded.p256dh, auth = excluded.auth, name = excluded.name",
                        (subscription_id, endpoint, p256dh, auth, name, int(time.time())))
        self.db.commit()
        return {"id": subscription_id, "name": name}

    def remove(self, subscription_id: str) -> bool:
        removed = self.db.execute("DELETE FROM push_subscriptions WHERE id = ?", (subscription_id,)).rowcount
        self.db.commit()
        return bool(removed)

    async def send(self, http, message: dict, only: str | None = None) -> int:
        """Encrypt `message` for each device (or just `only`) and post it; return how many accepted it."""
        payload = json.dumps(message, ensure_ascii=False).encode()
        if len(payload) > MAX_PAYLOAD:
            raise ValueError("push payload too large")
        rows = self.db.execute("SELECT id, endpoint, p256dh, auth FROM push_subscriptions" +
                               (" WHERE id = ?" if only else ""), (only,) if only else ()).fetchall()
        delivered = 0
        for subscription_id, endpoint, p256dh, auth in rows:
            headers = {"Authorization": vapid_authorization(self.key, endpoint, self.subject), "TTL": str(120 if message.get("approval") else TTL_SECONDS),
                       "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "Urgency": "high" if message.get("approval") else "normal"}
            try:
                async with http.post(endpoint, data=encrypt(payload, p256dh, auth), headers=headers,
                                     allow_redirects=False) as response:
                    status = response.status
            except Exception:  # an unreachable push service must not stop the others
                continue
            if status in (404, 410):
                # The browser unsubscribed or the subscription expired.
                self.remove(subscription_id)
            elif 200 <= status < 300:
                delivered += 1
                self.db.execute("UPDATE push_subscriptions SET last_sent = ? WHERE id = ?", (int(time.time()), subscription_id))
                self.db.commit()
        return delivered
