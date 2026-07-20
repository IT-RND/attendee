import base64
import hashlib
import json
import os
import re
from typing import Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DEEPLINK_SHARED_KEY = os.getenv("DEEPLINK_SHARED_KEY", "efkkFVKFidDZQ9CAPOsxh5Gy")
DEEPLINK_SECRET_KEY = os.getenv("DEEPLINK_SECRET_KEY", "0H3cfeah7Lss18xIokNV6jKQ")
DEEPLINK_SALT = os.getenv("DEEPLINK_SALT", "plD4VMd3QxVBQwhaHB7BkkoE")
DEEPLINK_PBKDF2_ITERATIONS = int(os.getenv("DEEPLINK_PBKDF2_ITERATIONS", "10000"))
DEEPLINK_EMPTY_PASSWORD_FALLBACK = os.getenv("DEEPLINK_EMPTY_PASSWORD_FALLBACK", "Jakarta2022")


class DeepLinkCredentialError(Exception):
    pass


def _normalize_user_id(value: str) -> str:
    return str(value or "").strip().lower()


def _derive_dotnet_rfc2898_key_iv() -> Optional[tuple[bytes, bytes]]:
    password = DEEPLINK_SECRET_KEY or ""
    salt = (DEEPLINK_SALT or "").encode("utf-8")
    if not password or not salt:
        return None
    iterations = DEEPLINK_PBKDF2_ITERATIONS if DEEPLINK_PBKDF2_ITERATIONS > 0 else 10000
    material = hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), salt, iterations, dklen=48)
    return material[:32], material[32:48]


def _derive_erp_salt_key() -> Optional[bytes]:
    """
    Match coresolution NotulenMeetingBot SaltEncryption: PBKDF2 derives AES key only.
    """
    password = DEEPLINK_SECRET_KEY or ""
    salt = (DEEPLINK_SALT or "").encode("utf-8")
    if not password or not salt:
        return None
    iterations = DEEPLINK_PBKDF2_ITERATIONS if DEEPLINK_PBKDF2_ITERATIONS > 0 else 10000
    return hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), salt, iterations, dklen=32)


def looks_like_encrypted_ciphertext(value: str) -> bool:
    encrypted_text = str(value or "").strip()
    if not encrypted_text:
        return False
    if re.fullmatch(r"[0-9a-fA-F]+", encrypted_text) and len(encrypted_text) % 2 == 0 and len(encrypted_text) >= 32:
        return True
    normalized = encrypted_text.replace("-", "+").replace("_", "/")
    return bool(re.fullmatch(r"[A-Za-z0-9+/=]+", normalized) and len(normalized) >= 24)


def _decode_user_ciphertext(encrypted_user: str) -> Optional[bytes]:
    encrypted_text = str(encrypted_user or "").strip()
    if not encrypted_text:
        return None
    if re.fullmatch(r"[0-9a-fA-F]+", encrypted_text) and len(encrypted_text) % 2 == 0:
        return bytes.fromhex(encrypted_text)
    normalized = encrypted_text.replace("-", "+").replace("_", "/")
    if re.fullmatch(r"[A-Za-z0-9+/=]+", normalized):
        try:
            return base64.b64decode(normalized)
        except ValueError:
            return None
    return None


def _pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    if not data or len(data) % block_size != 0:
        raise ValueError("Invalid padded data")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > block_size:
        raise ValueError("Invalid padding")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("Invalid padding")
    return data[:-pad_len]


def _try_decrypt_hex_payload(encrypted_user: str) -> str:
    payload = _decode_user_ciphertext(encrypted_user)
    if not payload:
        return ""
    derived = _derive_dotnet_rfc2898_key_iv()
    if not derived:
        return ""
    key, iv = derived
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(payload) + decryptor.finalize()
        decrypted = _pkcs7_unpad(decrypted)
        return decrypted.decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError):
        return ""


def _try_decrypt_erp_salt_payload(encrypted_user: str) -> str:
    """
    Decrypt ERP iframe userid values from NotulenMeetingBot SaltEncryption.

    Payload layout: 16-byte random IV + AES-CBC ciphertext; key from PBKDF2 only.
    """
    payload = _decode_user_ciphertext(encrypted_user)
    if not payload or len(payload) <= 16:
        return ""
    key = _derive_erp_salt_key()
    if not key:
        return ""
    iv = payload[:16]
    cipher_bytes = payload[16:]
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(cipher_bytes) + decryptor.finalize()
        decrypted = _pkcs7_unpad(decrypted)
        return decrypted.decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError):
        return ""


def _parse_deeplink_credentials(raw: str) -> Optional[tuple[str, str]]:
    text = str(raw or "").strip().replace("\ufeff", "")
    if not text:
        return None

    wrap_start = text.find("((")
    if wrap_start > 0:
        text = text[wrap_start:].strip()

    paren_wrapped = re.fullmatch(r"\(\(([\s\S]*)\)\)\s*", text)
    if paren_wrapped:
        inner = paren_wrapped.group(1).strip()
        if ";" in inner:
            user_raw, password = inner.split(";", 1)
            user_id = _normalize_user_id(user_raw)
            if user_id and re.fullmatch(r"[a-z0-9._-]+", user_id, re.IGNORECASE):
                return user_id, password.strip()
        return None

    readable_tail = re.sub(r"[^\x20-\x7E]+", " ", text).strip().split()
    readable_tail = readable_tail[-1] if readable_tail else ""
    if ";" in readable_tail:
        tail_user, tail_password = readable_tail.split(";", 1)
        tail_user_id = _normalize_user_id(tail_user)
        if tail_user_id and re.fullmatch(r"[a-z0-9._-]+", tail_user_id, re.IGNORECASE):
            return tail_user_id, tail_password.strip()

    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
            user_id = _normalize_user_id(parsed.get("userId") or parsed.get("userid") or parsed.get("user") or parsed.get("username"))
            password = str(parsed.get("password") or parsed.get("pass") or "").strip()
            if user_id:
                return user_id, password
        except json.JSONDecodeError:
            pass

    for delimiter in ("|", ";", ":", "~", ",", "="):
        if delimiter not in text:
            continue
        left, *rest = text.split(delimiter)
        left_clean = re.sub(r"[^\x20-\x7E]+", "", left).strip().lower()
        right_raw = delimiter.join(rest).strip()
        right_clean = re.sub(r"[^\x20-\x7E]+", "", right_raw).strip()
        if re.fullmatch(r"[a-z0-9._-]+", left_clean, re.IGNORECASE) and right_raw:
            return left_clean, right_raw
        if re.fullmatch(r"[a-z0-9._-]+", right_clean, re.IGNORECASE):
            return _normalize_user_id(right_clean), ""

    ascii_tail = re.sub(r"[^\x20-\x7E]+", " ", text).strip().split()
    ascii_tail = ascii_tail[-1].lower() if ascii_tail else ""
    if re.fullmatch(r"[a-z0-9._-]+", ascii_tail, re.IGNORECASE):
        return ascii_tail, ""

    return None


def _resolve_decrypted_credentials(decrypted: str) -> tuple[str, str]:
    parsed = _parse_deeplink_credentials(decrypted)
    if not parsed:
        raise DeepLinkCredentialError("Could not decrypt or parse user credentials from URL")

    user_id, password = parsed
    resolved_password = (password or "").strip() or DEEPLINK_EMPTY_PASSWORD_FALLBACK
    if not user_id:
        raise DeepLinkCredentialError("Decrypted user id is invalid")
    return user_id, resolved_password


def resolve_deeplink_credentials(encrypted_user: str, key: str) -> tuple[str, str]:
    normalized_key = str(key or "").strip()
    if not normalized_key:
        raise DeepLinkCredentialError("key is required")
    if normalized_key != DEEPLINK_SHARED_KEY:
        raise DeepLinkCredentialError("key is invalid")
    if not str(encrypted_user or "").strip():
        raise DeepLinkCredentialError("user is required")

    decrypted = _try_decrypt_hex_payload(encrypted_user)
    return _resolve_decrypted_credentials(decrypted)


def resolve_encrypted_userid_credentials(encrypted_userid: str) -> tuple[str, str]:
    """
    Decrypt a guest-session userid query parameter using DEEPLINK_SECRET_KEY + DEEPLINK_SALT.

    The decrypted payload is expected to contain ``userid;password`` for MeetingAI auth.
    """
    if not str(encrypted_userid or "").strip():
        raise DeepLinkCredentialError("userid is required")

    decrypted = _try_decrypt_erp_salt_payload(encrypted_userid)
    if not decrypted:
        raise DeepLinkCredentialError("Could not decrypt user credentials from URL")
    return _resolve_decrypted_credentials(decrypted)
