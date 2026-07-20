import base64
import logging
import os
import threading
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

MEETINGAI_API_BASE_URL = os.getenv("MEETINGAI_API_BASE_URL", "http://localhost:8080").rstrip("/")
MEETINGAI_BASIC_AUTH_B64 = os.getenv("MEETINGAI_BASIC_AUTH_B64", "")
MEETINGAI_BASIC_AUTH_USER = os.getenv("MEETINGAI_BASIC_AUTH_USER", "")
MEETINGAI_BASIC_AUTH_PASSWORD = os.getenv("MEETINGAI_BASIC_AUTH_PASSWORD", "")

MEETINGAI_REQUEST_TIMEOUT_SECONDS = 30

_token_lock = threading.Lock()
_token_cache: dict[str, dict[str, str]] = {}


class MeetingAIError(Exception):
    def __init__(self, message: str, *, code: Optional[str] = None, status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


def _meeting_ai_api_url(path: str) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{MEETINGAI_API_BASE_URL}/api/MeetingAI{normalized_path}"


def _meeting_ai_basic_auth_header() -> Optional[str]:
    if MEETINGAI_BASIC_AUTH_B64:
        return f"Basic {MEETINGAI_BASIC_AUTH_B64}"
    if MEETINGAI_BASIC_AUTH_USER and MEETINGAI_BASIC_AUTH_PASSWORD:
        encoded = base64.b64encode(
            f"{MEETINGAI_BASIC_AUTH_USER}:{MEETINGAI_BASIC_AUTH_PASSWORD}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {encoded}"
    return None


def from_meeting_ai_envelope(response: requests.Response) -> dict[str, Any]:
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        return {
            "ok": False,
            "status": status,
            "code": "INVALID_ENVELOPE",
            "message": "Invalid or non-JSON API response",
            "raw": response.text,
        }

    if not isinstance(body, dict) or not isinstance(body.get("success"), bool):
        return {
            "ok": False,
            "status": status,
            "code": "INVALID_ENVELOPE",
            "message": "Invalid or non-JSON API response",
            "raw": body,
        }

    if not body["success"]:
        error = body.get("error") or {}
        return {
            "ok": False,
            "status": status,
            "code": error.get("code"),
            "message": error.get("message") or body.get("message") or "Request failed",
            "details": error.get("details"),
            "timestamp": body.get("timestamp"),
        }

    return {
        "ok": True,
        "status": status,
        "data": body.get("data"),
        "message": body.get("message"),
        "timestamp": body.get("timestamp"),
    }


def _call_meeting_ai(method: str, path: str, *, json_body: Optional[dict] = None, headers: Optional[dict] = None) -> dict[str, Any]:
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)

    try:
        response = requests.request(
            method=method,
            url=_meeting_ai_api_url(path),
            json=json_body,
            headers=request_headers,
            timeout=MEETINGAI_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.exception("MeetingAI request failed for %s %s", method, path)
        return {
            "ok": False,
            "status": 0,
            "code": "NETWORK_ERROR",
            "message": str(exc) or "Network error",
        }

    return from_meeting_ai_envelope(response)


def _auth_headers_for_gateway() -> dict[str, str]:
    headers: dict[str, str] = {}
    basic_auth = _meeting_ai_basic_auth_header()
    if basic_auth:
        headers["Authorization"] = basic_auth
    return headers


def _get_cached_tokens(userid: str) -> tuple[str, str]:
    with _token_lock:
        entry = _token_cache.get(userid) or {}
        return entry.get("access_token", ""), entry.get("refresh_token", "")


def _set_cached_tokens(userid: str, *, access_token: str = "", refresh_token: str = "") -> None:
    with _token_lock:
        entry = _token_cache.setdefault(userid, {"access_token": "", "refresh_token": ""})
        if access_token:
            entry["access_token"] = access_token
        if refresh_token:
            entry["refresh_token"] = refresh_token


def _clear_cached_tokens(userid: str) -> None:
    with _token_lock:
        _token_cache.pop(userid, None)


def _apply_tokens_from_payload(userid: str, payload: Optional[dict]) -> None:
    if not isinstance(payload, dict):
        return
    access_token = payload.get("token") or payload.get("jwt_token") or ""
    refresh_token = payload.get("refresh_token") or ""
    _set_cached_tokens(userid, access_token=access_token, refresh_token=refresh_token)


def meeting_ai_login(*, userid: str, password: str) -> dict[str, Any]:
    result = _call_meeting_ai(
        "POST",
        "/auth/login",
        json_body={"userid": userid, "password": password},
        headers=_auth_headers_for_gateway(),
    )
    if result["ok"]:
        _apply_tokens_from_payload(userid, result.get("data"))
    return result


def meeting_ai_refresh(*, userid: str, refresh_token: str) -> dict[str, Any]:
    result = _call_meeting_ai(
        "POST",
        "/auth/refresh",
        json_body={"refresh_token": refresh_token},
        headers=_auth_headers_for_gateway(),
    )
    if result["ok"]:
        _apply_tokens_from_payload(userid, result.get("data"))
    return result


def meeting_ai_check(*, userid: str, access_token: Optional[str] = None) -> dict[str, Any]:
    headers = _auth_headers_for_gateway()
    token = access_token or _get_cached_tokens(userid)[0]
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _call_meeting_ai("POST", "/auth/check", json_body={}, headers=headers)


def _ensure_meeting_ai_auth(userid: str, password: str) -> dict[str, Any]:
    normalized_userid = (userid or "").strip().lower()
    if not normalized_userid or not password:
        return {
            "ok": False,
            "code": "MISSING_CREDENTIALS",
            "message": "MeetingAI userid and password are required",
        }

    access_token, refresh_token = _get_cached_tokens(normalized_userid)

    if access_token:
        check_result = meeting_ai_check(userid=normalized_userid, access_token=access_token)
        if check_result.get("ok"):
            jwt_token = (check_result.get("data") or {}).get("jwt_token")
            if jwt_token:
                _set_cached_tokens(normalized_userid, access_token=jwt_token)
            return {"ok": True}

    if refresh_token:
        refresh_result = meeting_ai_refresh(userid=normalized_userid, refresh_token=refresh_token)
        if refresh_result.get("ok") and (refresh_result.get("data") or {}).get("token"):
            return {"ok": True}
        _clear_cached_tokens(normalized_userid)

    login_result = meeting_ai_login(userid=normalized_userid, password=password)
    if not login_result.get("ok"):
        return login_result
    if not _get_cached_tokens(normalized_userid)[0]:
        return {
            "ok": False,
            "code": "LOGIN_NO_TOKEN",
            "message": "Login succeeded but no access token returned",
        }
    return {"ok": True}


def _call_meeting_ai_protected(
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    userid: str,
    password: str,
) -> dict[str, Any]:
    normalized_userid = userid.strip().lower()
    auth = _ensure_meeting_ai_auth(normalized_userid, password)
    if not auth.get("ok"):
        return {
            "ok": False,
            "status": 401,
            "code": auth.get("code") or "UNAUTHORIZED",
            "message": auth.get("message") or "Authentication required",
        }

    access_token, _ = _get_cached_tokens(normalized_userid)
    result = _call_meeting_ai(
        method,
        path,
        json_body=json_body,
        headers={"Authorization": f"Bearer {access_token}"},
    )

    if result.get("ok") or result.get("status") != 401:
        return result

    _clear_cached_tokens(normalized_userid)
    auth = _ensure_meeting_ai_auth(normalized_userid, password)
    if not auth.get("ok"):
        return {
            "ok": False,
            "status": 401,
            "code": auth.get("code") or "UNAUTHORIZED",
            "message": auth.get("message") or "Authentication required",
        }

    access_token, _ = _get_cached_tokens(normalized_userid)
    return _call_meeting_ai(
        method,
        path,
        json_body=json_body,
        headers={"Authorization": f"Bearer {access_token}"},
    )


def meeting_ai_create_meeting(body: dict[str, Any], *, userid: str, password: str) -> dict[str, Any]:
    return _call_meeting_ai_protected("POST", "/meeting/create", json_body=body, userid=userid, password=password)


def meeting_ai_edit_meeting(body: dict[str, Any], *, userid: str, password: str) -> dict[str, Any]:
    return _call_meeting_ai_protected("POST", "/meeting/edit", json_body=body, userid=userid, password=password)


def _raise_for_meeting_ai_result(result: dict[str, Any], action: str) -> dict[str, Any]:
    if result.get("ok"):
        data = result.get("data")
        if not isinstance(data, dict):
            raise MeetingAIError(
                f"MeetingAI {action} succeeded but returned no data payload",
                code="INVALID_RESPONSE",
                status=result.get("status") or 0,
            )
        return data

    raise MeetingAIError(
        result.get("message") or f"MeetingAI {action} failed",
        code=result.get("code"),
        status=result.get("status") or 0,
    )


def submit_meeting_to_meeting_ai(
    *,
    title: str,
    session_id: str,
    bot_id: str,
    transaction_date: str,
    meeting_ai_userid: str,
    meeting_ai_password: str,
    existing_transaction_id: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """
    Create or update a MeetingAI meeting record and return transaction_id.

    When existing_transaction_id is set, calls meeting/edit; otherwise meeting/create.
    """
    normalized_title = (title or "").strip() or "Meeting"
    existing_trxid = (existing_transaction_id or "").strip()

    if existing_trxid:
        edit_body: dict[str, Any] = {
            "trxid": existing_trxid,
            "title": normalized_title,
        }
        if transaction_date:
            edit_body["transaction_date"] = transaction_date
        data = _raise_for_meeting_ai_result(
            meeting_ai_edit_meeting(edit_body, userid=meeting_ai_userid, password=meeting_ai_password),
            "edit",
        )
    else:
        create_body = {
            "title": normalized_title,
            "session_id": session_id,
            "bot_id": bot_id,
            "transaction_date": transaction_date,
            "type": "Bot",
        }
        if status:
            create_body["status"] = status
        data = _raise_for_meeting_ai_result(
            meeting_ai_create_meeting(create_body, userid=meeting_ai_userid, password=meeting_ai_password),
            "create",
        )

    transaction_id = (data.get("transaction_id") or "").strip()
    if not transaction_id:
        raise MeetingAIError(
            "MeetingAI response did not include transaction_id",
            code="MISSING_TRANSACTION_ID",
        )

    logger.info(
        "MeetingAI meeting %s for session %s: transaction_id=%s",
        "updated" if existing_trxid else "created",
        session_id,
        transaction_id,
    )
    return transaction_id
