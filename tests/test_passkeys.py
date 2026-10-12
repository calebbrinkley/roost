"""Passkey registration and sign-in against a software authenticator with real signatures."""

import hashlib
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from roost import server
from roost.auth import Sessions, make_password_hash
from roost.passkeys import PasskeyError, Passkeys, b64url, unb64url

ORIGIN, RP_ID = "https://roost.example.invalid", "roost.example.invalid"


def cbor(value) -> bytes:
    """Just enough CBOR to build authenticator responses."""
    def head(major, argument):
        if argument < 24:
            return bytes([major << 5 | argument])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if argument < 1 << (8 * size):
                return bytes([major << 5 | info]) + argument.to_bytes(size, "big")
    if isinstance(value, bool):
        return bytes([0xF5 if value else 0xF4])
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        return head(3, len(value.encode())) + value.encode()
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(value)


class Authenticator:
    """A security key in software: one ES256 or EdDSA credential, user-verified by default."""

    def __init__(self, algorithm="ES256", rp_id=RP_ID, origin=ORIGIN):
        self.algorithm, self.rp_id, self.origin = algorithm, rp_id, origin
        self.key = ec.generate_private_key(ec.SECP256R1()) if algorithm == "ES256" else ed25519.Ed25519PrivateKey.generate()
        self.credential_id = os.urandom(32)
        self.count = 0
        self.flags = 0x01 | 0x04

    def cose(self):
        public = self.key.public_key()
        if self.algorithm == "ES256":
            numbers = public.public_numbers()
            return {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        from cryptography.hazmat.primitives import serialization
        raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {1: 1, 3: -8, -1: 6, -2: raw}

    def client_data(self, kind, challenge, origin=None):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin, "crossOrigin": False}).encode()

    def create(self, options, origin=None):
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([self.flags | 0x40]) + struct.pack(">I", self.count)
                     + bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + cbor(self.cose()))
        return {"id": b64url(self.credential_id), "type": "public-key",
                "clientDataJSON": b64url(self.client_data("webauthn.create", options["challenge"], origin)),
                "attestationObject": b64url(cbor({"fmt": "none", "attStmt": {}, "authData": auth_data}))}

    def get(self, options, user_handle=None, origin=None):
        self.count += 1 if self.count or self.algorithm == "ES256" else 0
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([self.flags]) + struct.pack(">I", self.count)
        client = self.client_data("webauthn.get", options["challenge"], origin)
        signed = auth_data + hashlib.sha256(client).digest()
        signature = self.key.sign(signed, ec.ECDSA(hashes.SHA256())) if self.algorithm == "ES256" else self.key.sign(signed)
        return {"id": b64url(self.credential_id), "clientDataJSON": b64url(client), "authenticatorData": b64url(auth_data),
                "signature": b64url(signature), "userHandle": user_handle}


class PasskeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.sessions = Sessions(Path(self.directory.name) / "sessions.sqlite3")
        self.passkeys = Passkeys(self.sessions.db, ORIGIN, RP_ID)

    def tearDown(self):
        self.sessions.close()
        self.directory.cleanup()

    def registered(self, algorithm="ES256"):
        device = Authenticator(algorithm)
        self.passkeys.register(device.create(self.passkeys.registration_options("session")), "session", "iPhone")
        return device

    def test_register_then_sign_in_with_es256_and_ed25519(self):
        for algorithm in ("ES256", "EdDSA"):
            with self.subTest(algorithm=algorithm):
                device = self.registered(algorithm)
                handle = b64url(self.passkeys.owner_handle())
                self.passkeys.authenticate(device.get(self.passkeys.authentication_options(), handle))
                self.passkeys.authenticate(device.get(self.passkeys.authentication_options()))
        rows = self.passkeys.list()
        self.assertEqual([row["name"] for row in rows], ["iPhone", "iPhone"])
        self.assertTrue(all(row["last_used"] for row in rows))

    def test_registration_rejects_wrong_origin_session_site_and_unverified_users(self):
        cases = [
            ("origin", lambda d, o: d.create(o, origin="https://evil.example.invalid"), "another site"),
            ("session", lambda d, o: d.create(o), "expired"),
            ("rp", lambda d, o: Authenticator(rp_id="evil.example.invalid").create(o), "belongs to another site"),
        ]
        for label, respond, message in cases:
            with self.subTest(label):
                options = self.passkeys.registration_options("other-session" if label == "session" else "session")
                with self.assertRaisesRegex(PasskeyError, message):
                    self.passkeys.register(respond(Authenticator(), options), "session", "Mac")
        device = Authenticator()
        device.flags = 0x01  # present, not verified
        with self.assertRaisesRegex(PasskeyError, "confirm it's you"):
            self.passkeys.register(device.create(self.passkeys.registration_options("session")), "session", "Mac")
        self.assertEqual(self.passkeys.count(), 0)

    def test_sign_in_rejects_replay_bad_signature_wrong_owner_and_cloned_counter(self):
        device = self.registered()
        options = self.passkeys.authentication_options()
        response = device.get(options)
        self.passkeys.authenticate(response)
        with self.assertRaisesRegex(PasskeyError, "expired"):
            self.passkeys.authenticate(response)  # the challenge is single use
        tampered = device.get(self.passkeys.authentication_options())
        tampered["signature"] = b64url(unb64url(tampered["signature"])[:-1] + b"\0")
        with self.assertRaises(PasskeyError):
            self.passkeys.authenticate(tampered)
        with self.assertRaisesRegex(PasskeyError, "someone else"):
            self.passkeys.authenticate(device.get(self.passkeys.authentication_options(), b64url(b"x" * 32)))
        device.count = 0  # a copy that restarted its counter
        device.count -= 1
        with self.assertRaisesRegex(PasskeyError, "copied"):
            self.passkeys.authenticate(device.get(self.passkeys.authentication_options()))
        stranger = Authenticator()
        with self.assertRaisesRegex(PasskeyError, "isn't set up"):
            self.passkeys.authenticate(stranger.get(self.passkeys.authentication_options()))

    def test_synced_passkeys_with_a_zero_counter_keep_working(self):
        device = self.registered("EdDSA")  # count stays 0, like iCloud Keychain
        for _ in range(3):
            self.passkeys.authenticate(device.get(self.passkeys.authentication_options()))

    def test_malformed_responses_are_passkey_errors(self):
        options = self.passkeys.registration_options("session")
        for broken in ({}, {"clientDataJSON": "!!"}, {"clientDataJSON": b64url(b"[]")},
                       {"clientDataJSON": b64url(json.dumps({"type": "webauthn.create", "challenge": options["challenge"], "origin": ORIGIN}).encode()),
                        "attestationObject": b64url(b"\xbf\xff")}):
            with self.subTest(broken=broken), self.assertRaises(PasskeyError):
                self.passkeys.register(broken, "session", "Mac")
            options = self.passkeys.registration_options("session")


class PasskeyHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name)
        (self.state / "password.hash").write_text(make_password_hash("example-demo-password"))
        with patch.dict(os.environ, {
            "ROOST_PUBLIC_ORIGIN": ORIGIN, "ROOST_STATE_DIR": str(self.state),
            "ROOST_HERMES_URL": "http://127.0.0.1:1", "ROOST_HERMES_USERNAME": "demo",
            "ROOST_HERMES_PASSWORD_FILE": str(self.state / "upstream-password"),
        }):
            self.app = server.create_app()
        self.app["hermes"] = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), request=AsyncMock(return_value=[]))
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.headers = {"Origin": ORIGIN, "X-Roost-Request": "1"}

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def post(self, path, body=None, cookie=None):
        response = await self.client.post(path, headers=self.headers, json=body or {},
                                          cookies={server.COOKIE: cookie} if cookie else None)
        return response.status, await response.json()

    async def test_add_a_passkey_while_signed_in_then_sign_in_with_it(self):
        status, _ = await self.post("/api/passkeys/register/options")
        self.assertEqual(status, 401)  # adding needs a session
        response = await self.client.post("/api/login", headers=self.headers, json={"password": "example-demo-password"})
        session = response.cookies[server.COOKIE].value
        status, options = await self.post("/api/passkeys/register/options", cookie=session)
        self.assertEqual((status, options["rp"]["id"], options["authenticatorSelection"]["userVerification"]), (200, RP_ID, "required"))
        device = Authenticator()
        status, added = await self.post("/api/passkeys", {**device.create(options), "name": "iPhone"}, cookie=session)
        self.assertEqual((status, added["name"]), (201, "iPhone"))
        me = await (await self.client.get("/api/me")).json()
        self.assertTrue(me["passkeys"])
        status, options = await self.post("/api/passkeys/login/options")
        status, signed_in = await self.post("/api/passkeys/login", device.get(options))
        self.assertEqual((status, signed_in["signed_in"]), (200, True))

    async def test_failed_passkey_sign_ins_count_toward_the_device_limit(self):
        stranger = Authenticator()
        for _ in range(server.DEVICE_FAILURES):
            _, options = await self.post("/api/passkeys/login/options")
            status, body = await self.post("/api/passkeys/login", stranger.get(options))
            self.assertEqual(status, 401, body)
        _, options = await self.post("/api/passkeys/login/options")
        status, _ = await self.post("/api/passkeys/login", stranger.get(options))
        self.assertEqual(status, 429)
        self.assertEqual(self.app["login_inflight"], {})

    async def test_removing_a_passkey_needs_confirmation(self):
        response = await self.client.post("/api/login", headers=self.headers, json={"password": "example-demo-password"})
        session = response.cookies[server.COOKIE].value
        _, options = await self.post("/api/passkeys/register/options", cookie=session)
        device = Authenticator()
        await self.post("/api/passkeys", {**device.create(options), "name": "Mac"}, cookie=session)
        pid = b64url(device.credential_id)
        for body in ({}, {"confirm": "yes"}):
            response = await self.client.delete("/api/passkeys/" + pid, headers=self.headers, json=body,
                                                cookies={server.COOKIE: session})
            self.assertEqual(response.status, 400)
        response = await self.client.delete("/api/passkeys/" + pid, headers=self.headers, json={"confirm": True},
                                            cookies={server.COOKIE: session})
        self.assertEqual(response.status, 200)
        listed = await (await self.client.get("/api/passkeys", cookies={server.COOKIE: session})).json()
        self.assertEqual(listed["passkeys"], [])


if __name__ == "__main__":
    unittest.main()
