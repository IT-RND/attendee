import os
from urllib.parse import urlencode

import requests
from django.core import signing
from django.urls import reverse

from bots.bots_api_utils import build_site_url
from bots.calendars_api_utils import create_calendar
from bots.models import Calendar, CalendarPlatform, CalendarStates, Project
from bots.tasks.sync_calendar_task import enqueue_sync_calendar_task

GOOGLE_CALENDAR_OAUTH_SCOPE = "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/userinfo.email"
GOOGLE_CALENDAR_STATE_SALT = "google-calendar-oauth"


class GoogleCalendarOAuthError(Exception):
    pass


def google_calendar_oauth_is_configured() -> bool:
    return bool(_google_calendar_client_id() and _google_calendar_client_secret())


def build_google_calendar_oauth_authorize_url(project: Project) -> str:
    client_id = _google_calendar_client_id()
    if not client_id:
        raise GoogleCalendarOAuthError("Google Calendar OAuth is not configured.")

    state = signing.dumps(
        {"project_object_id": project.object_id},
        salt=GOOGLE_CALENDAR_STATE_SALT,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": google_calendar_oauth_redirect_uri(),
        "response_type": "code",
        "scope": GOOGLE_CALENDAR_OAUTH_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


def get_project_object_id_from_state(state: str) -> str:
    try:
        state_data = signing.loads(
            state,
            salt=GOOGLE_CALENDAR_STATE_SALT,
            max_age=600,
        )
    except signing.BadSignature as exc:
        raise GoogleCalendarOAuthError("The Google Calendar authorization link is invalid or expired.") from exc

    project_object_id = state_data.get("project_object_id")
    if not project_object_id:
        raise GoogleCalendarOAuthError("The Google Calendar authorization state is missing the project id.")

    return project_object_id


def connect_google_calendar(project: Project, authorization_code: str) -> Calendar:
    client_id = _google_calendar_client_id()
    client_secret = _google_calendar_client_secret()
    if not client_id or not client_secret:
        raise GoogleCalendarOAuthError("Google Calendar OAuth is not configured.")

    token_data = _exchange_access_code_for_tokens(
        authorization_code=authorization_code,
        client_id=client_id,
        client_secret=client_secret,
    )
    refresh_token = token_data.get("refresh_token")
    access_token = token_data.get("access_token")
    if not refresh_token:
        raise GoogleCalendarOAuthError(
            "Google did not return a refresh token. Remove this app from your Google account and try connecting again."
        )
    if not access_token:
        raise GoogleCalendarOAuthError("Google did not return an access token.")

    authorized_email = _fetch_google_user_email(access_token)

    calendar = Calendar.objects.filter(
        project=project,
        platform=CalendarPlatform.GOOGLE,
        deduplication_key=authorized_email,
    ).first()

    if calendar:
        metadata = dict(calendar.metadata or {})
        metadata["authorized_email"] = authorized_email
        calendar.client_id = client_id
        calendar.metadata = metadata
        calendar.state = CalendarStates.CONNECTED
        calendar.connection_failure_data = None
        calendar.save(update_fields=["client_id", "metadata", "state", "connection_failure_data", "updated_at"])
        calendar.set_credentials(
            {
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            }
        )
    else:
        calendar, error = create_calendar(
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "platform": CalendarPlatform.GOOGLE,
                "metadata": {"authorized_email": authorized_email},
                "deduplication_key": authorized_email,
            },
            project=project,
        )
        if error:
            raise GoogleCalendarOAuthError(_format_calendar_create_error(error))

    enqueue_sync_calendar_task(calendar)
    return calendar


def google_calendar_oauth_redirect_uri() -> str:
    return build_site_url(reverse("projects:project-google-calendar-oauth-callback"))


def _google_calendar_client_id() -> str | None:
    value = os.getenv("GOOGLE_CALENDAR_OAUTH_CLIENT_ID", "").strip()
    return value or None


def _google_calendar_client_secret() -> str | None:
    value = os.getenv("GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET", "").strip()
    return value or None


def _exchange_access_code_for_tokens(authorization_code: str, client_id: str, client_secret: str) -> dict:
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": authorization_code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": google_calendar_oauth_redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise GoogleCalendarOAuthError(_format_google_response_error(response, "Could not exchange the Google authorization code."))
    return response.json()


def _fetch_google_user_email(access_token: str) -> str:
    response = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if response.status_code >= 400:
        raise GoogleCalendarOAuthError(_format_google_response_error(response, "Could not fetch the Google account email."))

    email = response.json().get("email")
    if not email:
        raise GoogleCalendarOAuthError("Google did not return an email address for this account.")
    return email


def _format_google_response_error(response: requests.Response, fallback_message: str) -> str:
    try:
        body = response.json()
    except ValueError:
        body = response.text

    if isinstance(body, dict):
        error_description = body.get("error_description")
        if error_description:
            return error_description

        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return message
        elif error:
            return str(error)

    if isinstance(body, str) and body.strip():
        return body.strip()

    return fallback_message


def _format_calendar_create_error(error: dict) -> str:
    messages = []
    for value in error.values():
        if isinstance(value, list):
            messages.extend(str(item) for item in value)
        else:
            messages.append(str(value))
    return " ".join(messages) if messages else "Could not create the Google calendar."
