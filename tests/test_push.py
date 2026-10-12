"""Web Push encryption, VAPID and delivery."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from roost.auth import Sessions
from roost.push import Push, PushError, allowed_endpoint, b64url, encrypt, unb64url, vapid_authorization

# RFC 8291 section 5 and appendix A, with the RFC's own whitespace removed.
RFC = {
    "plaintext": "V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24",
    "as_private": "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
    "ua_public": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
    "salt": "DGv6ra1nlYgDCS1FRnbzlw",
    "auth": "BTBZMqHH6r4Tts7J_aSIgg",
    "header": "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8",
    "ciphertext": "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ",
}


class EncryptionTests(unittest.TestCase):
    def test_matches_the_rfc_8291_example_byte_for_byte(self):
        as_private = ec.derive_private_key(int.from_bytes(unb64url(RFC["as_private"]), "big"), ec.SECP256R1())
        body = encrypt(unb64url(RFC["plaintext"]), unb64url(RFC["ua_public"]), unb64url(RFC["auth"]),
                       salt=unb64url(RFC["salt"]), as_private=as_private)
        # The RFC prints the 86-byte header and the ciphertext separately; neither ends on a base64 boundary.
        self.assertEqual(body[:86], unb64url(RFC["header"]))
        self.assertEqual(body[86:], unb64url(RFC["ciphertext"]))

    def test_vapid_token_is_a_verifiable_es256_jwt_for_the_push_origin(self):
        key = ec.generate_private_key(ec.SECP256R1())
        value = vapid_authorization(key, "https://web.push.apple.com/abc/def", "https://roost.example.invalid", now=1000)
        token, public = value.removeprefix("vapid t=").split(", k=")
        header, claims, signature = token.split(".")
        self.assertEqual(json.loads(unb64url(claims)), {"aud": "https://web.push.apple.com", "exp": 1000 + 43200,
                                                        "sub": "https://roost.example.invalid"})
        raw = unb64url(signature)
        key.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                                f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        self.assertEqual(len(unb64url(public)), 65)

    def test_only_browser_push_services_are_accepted(self):
        for url, ok in (("https://web.push.apple.com/QW", True), ("https://fcm.googleapis.com/fcm/send/x", True),
                        ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
                        ("https://wns2-bl2p.notify.windows.com/w/?token=x", True),
                        ("http://web.push.apple.com/QW", False), ("https://push.apple.com.evil.example/x", False),
                        ("https://skynet-hermes:9119/api/env", False), ("https://user:@web.push.apple.com/x", False),
                        ("https://web.push.apple.com:8443/x", False)):
            with self.subTest(url=url):
                self.assertEqual(allowed_endpoint(url), ok)


class FakeResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeHTTP:
    def __init__(self, statuses):
        self.statuses, self.posts = statuses, []

    def post(self, url, data, headers, allow_redirects):
        self.posts.append((url, data, headers))
        return FakeResponse(self.statuses.get(url, 201))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        state = Path(self.directory.name)
        self.sessions = Sessions(state / "sessions.sqlite3")
        self.push = Push(self.sessions.db, state / "vapid.pem", "https://roost.example.invalid")

    def tearDown(self):
        self.sessions.close()
        self.directory.cleanup()

    def browser(self, endpoint):
        key = ec.generate_private_key(ec.SECP256R1())
        from roost.push import point
        return {"endpoint": endpoint, "keys": {"p256dh": b64url(point(key.public_key())), "auth": b64url(b"a" * 16)}}

    def test_vapid_key_is_created_once_and_kept_private(self):
        path = Path(self.directory.name) / "vapid.pem"
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        again = Push(self.sessions.db, path, "https://roost.example.invalid")
        self.assertEqual(again.public_key(), self.push.public_key())

    def test_subscriptions_are_validated_renewed_in_place_and_removed_when_gone(self):
        with self.assertRaisesRegex(PushError, "isn't one Roost sends to"):
            self.push.subscribe(self.browser("https://skynet-hermes:9119/x"), "iPhone")
        bad = self.browser("https://web.push.apple.com/a")
        bad["keys"]["p256dh"] = b64url(b"\x04" + b"\x01" * 64)
        with self.assertRaisesRegex(PushError, "unusable"):
            self.push.subscribe(bad, "iPhone")
        first = self.push.subscribe(self.browser("https://web.push.apple.com/a"), "iPhone")
        self.push.subscribe(self.browser("https://web.push.apple.com/a"), "iPhone (renewed)")
        self.push.subscribe(self.browser("https://fcm.googleapis.com/fcm/send/b"), "Pixel")
        self.assertEqual([row["name"] for row in self.push.list()], ["iPhone (renewed)", "Pixel"])
        http = FakeHTTP({"https://fcm.googleapis.com/fcm/send/b": 410})
        delivered = asyncio.run(self.push.send(http, {"title": "Kestrel", "body": "Done.", "url": "/"}))
        self.assertEqual(delivered, 1)
        self.assertEqual([row["id"] for row in self.push.list()], [first["id"]])
        url, body, headers = http.posts[0]
        self.assertEqual((headers["Content-Encoding"], headers["TTL"]), ("aes128gcm", "86400"))
        self.assertTrue(headers["Authorization"].startswith("vapid t="))
        self.assertNotIn(b"Done.", body)  # encrypted
        self.assertIsNotNone(self.push.list()[0]["last_sent"])

    def test_oversized_messages_are_refused_before_sending(self):
        self.push.subscribe(self.browser("https://web.push.apple.com/a"), "iPhone")
        http = FakeHTTP({})
        with self.assertRaises(ValueError):
            asyncio.run(self.push.send(http, {"body": "x" * 4000}))
        self.assertEqual(http.posts, [])


if __name__ == "__main__":
    unittest.main()
