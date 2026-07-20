import hashlib
from unittest.mock import patch

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.test import RequestFactory, SimpleTestCase, TestCase

from bots.meeting_ai_credentials_utils import resolve_meeting_ai_credentials_from_request
from bots.meeting_ai_deeplink_utils import (
    DeepLinkCredentialError,
    looks_like_encrypted_ciphertext,
    resolve_deeplink_credentials,
    resolve_encrypted_userid_credentials,
)


def _encrypt_erp_salt_style(plaintext: str, *, secret_key: str, salt: str, iv: bytes, iterations: int = 10000) -> str:
    key = hashlib.pbkdf2_hmac("sha1", secret_key.encode("utf-8"), salt.encode("utf-8"), iterations, dklen=32)

    data = plaintext.encode("utf-8")
    pad_len = 16 - (len(data) % 16)
    data += bytes([pad_len]) * pad_len

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(data) + encryptor.finalize()
    return (iv + encrypted).hex().upper()


def _encrypt_dotnet_style(plaintext: str, *, secret_key: str, salt: str, iterations: int = 10000) -> str:
    material = hashlib.pbkdf2_hmac("sha1", secret_key.encode("utf-8"), salt.encode("utf-8"), iterations, dklen=48)
    key, iv = material[:32], material[32:48]

    data = plaintext.encode("utf-8")
    pad_len = 16 - (len(data) % 16)
    data += bytes([pad_len]) * pad_len

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(data) + encryptor.finalize()
    return encrypted.hex().upper()


class ResolveEncryptedUseridCredentialsTest(TestCase):
    def test_hex_userid_query_param_looks_encrypted(self):
        encrypted = "DEF3318935E11F045B72F0E2FF1189EC2B262FF463CB5FE9481CC341952DCD26"
        self.assertTrue(looks_like_encrypted_ciphertext(encrypted))
        self.assertFalse(looks_like_encrypted_ciphertext("alice.example"))

    @patch.dict("os.environ", {}, clear=False)
    def test_decrypts_userid_password_payload(self):
        with patch.multiple("bots.meeting_ai_deeplink_utils", **{
            "DEEPLINK_SECRET_KEY": "test-secret-key",
            "DEEPLINK_SALT": "test-salt",
        }):
            encrypted_userid = _encrypt_erp_salt_style(
                "\ufeffalice.example;secret-pass",
                secret_key="test-secret-key",
                salt="test-salt",
                iv=bytes.fromhex("00112233445566778899aabbccddeeff"),
            )

            userid, password = resolve_encrypted_userid_credentials(encrypted_userid)

        self.assertEqual(userid, "alice.example")
        self.assertEqual(password, "secret-pass")

    @patch.dict("os.environ", {}, clear=False)
    def test_uses_empty_password_fallback(self):
        with patch.multiple("bots.meeting_ai_deeplink_utils", **{
            "DEEPLINK_SECRET_KEY": "test-secret-key",
            "DEEPLINK_SALT": "test-salt",
        }):
            encrypted_userid = _encrypt_erp_salt_style(
                "\ufeffalice.example;",
                secret_key="test-secret-key",
                salt="test-salt",
                iv=bytes.fromhex("00112233445566778899aabbccddeeff"),
            )

            userid, password = resolve_encrypted_userid_credentials(encrypted_userid)

        self.assertEqual(userid, "alice.example")
        self.assertEqual(password, "Jakarta2022")

    def test_raises_when_salt_not_configured(self):
        with patch.multiple("bots.meeting_ai_deeplink_utils", **{
            "DEEPLINK_SECRET_KEY": "test-secret-key",
            "DEEPLINK_SALT": "",
        }):
            with self.assertRaises(DeepLinkCredentialError):
                resolve_encrypted_userid_credentials("deadbeefdeadbeefdeadbeefdeadbeef00")


class ResolveDeeplinkCredentialsTest(TestCase):
    @patch.dict("os.environ", {}, clear=False)
    def test_decrypts_user_and_key_payload(self):
        with patch.multiple("bots.meeting_ai_deeplink_utils", **{
            "DEEPLINK_SECRET_KEY": "test-secret-key",
            "DEEPLINK_SALT": "test-salt",
            "DEEPLINK_SHARED_KEY": "shared-key",
        }):
            encrypted_user = _encrypt_dotnet_style(
                "bob.example;another-pass",
                secret_key="test-secret-key",
                salt="test-salt",
            )

            userid, password = resolve_deeplink_credentials(encrypted_user, "shared-key")

        self.assertEqual(userid, "bob.example")
        self.assertEqual(password, "another-pass")


class ResolveMeetingAIEncryptedUseridRequestTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("bots.meeting_ai_credentials_utils.resolve_encrypted_userid_credentials")
    def test_resolve_encrypted_userid_from_get(self, mock_resolve):
        mock_resolve.return_value = ("alice.example", "secret")
        request = self.factory.get(
            "/projects/guest/session",
            data={"userid": "ENCRYPTED"},
        )

        userid, password = resolve_meeting_ai_credentials_from_request(request)

        self.assertEqual(userid, "alice.example")
        self.assertEqual(password, "secret")
        mock_resolve.assert_called_once_with("ENCRYPTED")

    @patch("bots.meeting_ai_credentials_utils.resolve_encrypted_userid_credentials")
    def test_resolve_encrypted_userid_from_post(self, mock_resolve):
        mock_resolve.return_value = ("alice.example", "secret")
        request = self.factory.post(
            "/projects/guest/session",
            data={"userid": "ENCRYPTED"},
        )

        userid, password = resolve_meeting_ai_credentials_from_request(request)

        self.assertEqual(userid, "alice.example")
        self.assertEqual(password, "secret")
        mock_resolve.assert_called_once_with("ENCRYPTED")

    def test_explicit_post_credentials_take_priority_over_encrypted_userid(self):
        request = self.factory.post(
            "/projects/guest/session",
            data={"userid": "alice.example", "password": "plain-secret"},
        )

        userid, password = resolve_meeting_ai_credentials_from_request(request)

        self.assertEqual(userid, "alice.example")
        self.assertEqual(password, "plain-secret")

    @patch("bots.meeting_ai_credentials_utils.resolve_encrypted_userid_credentials")
    def test_encrypted_userid_post_is_not_treated_as_plain_credentials(self, mock_resolve):
        encrypted = "DEF3318935E11F045B72F0E2FF1189EC2B262FF463CB5FE9481CC341952DCD26"
        mock_resolve.return_value = ("alice.example", "secret")
        request = self.factory.post(
            "/projects/guest/session",
            data={"userid": encrypted, "password": "ignored"},
        )

        userid, password = resolve_meeting_ai_credentials_from_request(request)

        self.assertEqual(userid, "alice.example")
        self.assertEqual(password, "secret")
        mock_resolve.assert_called_once_with(encrypted)
