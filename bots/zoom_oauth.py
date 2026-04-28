from urllib.parse import urlencode

from django.core import signing
from django.urls import reverse

from bots.bots_api_utils import build_site_url
from bots.models import Project, ZoomOAuthConnection
from bots.tasks.sync_zoom_oauth_connection_task import enqueue_sync_zoom_oauth_connection_task
from bots.zoom_oauth_connections_api_utils import create_zoom_oauth_connection

ZOOM_OAUTH_STATE_SALT = "zoom-oauth"


class ZoomOAuthError(Exception):
    pass


def build_zoom_oauth_authorize_url(
    project: Project,
    *,
    is_local_recording_token_supported: bool = False,
    is_onbehalf_token_supported: bool = True,
) -> str:
    zoom_oauth_app = project.zoom_oauth_apps.first()
    if not zoom_oauth_app:
        raise ZoomOAuthError("Add Zoom OAuth App credentials before connecting a Zoom account.")

    state = signing.dumps(
        {
            "project_object_id": project.object_id,
            "is_local_recording_token_supported": is_local_recording_token_supported,
            "is_onbehalf_token_supported": is_onbehalf_token_supported,
        },
        salt=ZOOM_OAUTH_STATE_SALT,
    )
    params = {
        "response_type": "code",
        "client_id": zoom_oauth_app.client_id,
        "redirect_uri": zoom_oauth_redirect_uri(),
        "state": state,
    }
    return f"https://zoom.us/oauth/authorize?{urlencode(params)}"


def get_zoom_oauth_state_data(state: str) -> dict:
    try:
        state_data = signing.loads(
            state,
            salt=ZOOM_OAUTH_STATE_SALT,
            max_age=600,
        )
    except signing.BadSignature as exc:
        raise ZoomOAuthError("The Zoom authorization link is invalid or expired.") from exc

    if not state_data.get("project_object_id"):
        raise ZoomOAuthError("The Zoom authorization state is missing the project id.")

    return state_data


def connect_zoom_oauth_connection(project: Project, authorization_code: str, state_data: dict) -> ZoomOAuthConnection:
    zoom_oauth_connection, error = create_zoom_oauth_connection(
        data={
            "authorization_code": authorization_code,
            "redirect_uri": zoom_oauth_redirect_uri(),
            "is_local_recording_token_supported": state_data.get("is_local_recording_token_supported", False),
            "is_onbehalf_token_supported": state_data.get("is_onbehalf_token_supported", True),
        },
        project=project,
    )
    if error:
        raise ZoomOAuthError(_format_zoom_oauth_create_error(error))

    enqueue_sync_zoom_oauth_connection_task(zoom_oauth_connection)
    return zoom_oauth_connection


def zoom_oauth_redirect_uri() -> str:
    return build_site_url(reverse("projects:project-zoom-oauth-callback"))


def _format_zoom_oauth_create_error(error) -> str:
    if isinstance(error, dict):
        messages = []
        for field, value in error.items():
            if isinstance(value, list):
                value = " ".join(str(item) for item in value)
            messages.append(f"{field}: {value}" if field != "error" else str(value))
        return " ".join(messages)
    return str(error)
