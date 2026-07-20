from typing import Optional

from django.http import HttpRequest

from .meeting_ai_deeplink_utils import (
    DeepLinkCredentialError,
    looks_like_encrypted_ciphertext,
    resolve_deeplink_credentials,
    resolve_encrypted_userid_credentials,
)


def _first_non_empty(*values: Optional[str]) -> str:
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def resolve_meeting_ai_credentials_from_request(request: HttpRequest) -> tuple[Optional[str], Optional[str]]:
    """
    Resolve MeetingAI WebApps credentials from the guest session request.

    Priority:
    1. Explicit POST fields meeting_ai_userid/meeting_ai_password or userid/password
    2. ERP deeplink POST/GET fields user + key (encrypted payload from guest session URL)
    3. Encrypted userid POST/GET query param (decrypted with DEEPLINK_SALT; payload is userid;password)
    """
    post_userid = request.POST.get("userid")
    userid = _first_non_empty(
        request.POST.get("meeting_ai_userid"),
        post_userid if post_userid and not looks_like_encrypted_ciphertext(post_userid) else None,
    )
    password = _first_non_empty(
        request.POST.get("meeting_ai_password"),
        request.POST.get("password"),
    )
    if userid and password:
        return userid.lower(), password

    encrypted_user = _first_non_empty(
        request.POST.get("user"),
        request.GET.get("user"),
    )
    key = _first_non_empty(
        request.POST.get("key"),
        request.GET.get("key"),
    )
    if encrypted_user and key:
        try:
            return resolve_deeplink_credentials(encrypted_user, key)
        except DeepLinkCredentialError:
            return None, None

    encrypted_userid = _first_non_empty(
        request.GET.get("userid"),
        request.POST.get("userid"),
    )
    if encrypted_userid:
        try:
            return resolve_encrypted_userid_credentials(encrypted_userid)
        except DeepLinkCredentialError:
            return None, None

    return None, None


def get_encrypted_userid_from_request(request: HttpRequest) -> str:
    return _first_non_empty(
        request.GET.get("userid"),
        request.POST.get("userid"),
    )


def resolve_existing_transaction_id_from_request(request: HttpRequest) -> str:
    return _first_non_empty(
        request.POST.get("transaction_id"),
        request.POST.get("trxid"),
        request.GET.get("trxid"),
    )
