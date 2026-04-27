import secrets

from django.http import Http404
from django.urls import reverse

from accounts.models import UserRole
from bots.models import Bot, ProjectAccess, SessionTypes


def user_can_share_guest_mom_link(user, project) -> bool:
    """Org admins or users explicitly granted access to the project may copy the guest MoM link."""
    if not user.is_authenticated:
        return False
    if getattr(user, "role", None) == UserRole.ADMIN:
        return True
    return ProjectAccess.objects.filter(project=project, user=user).exists()


def new_mom_guest_token() -> str:
    return secrets.token_urlsafe(32)


def get_bot_for_guest_mom(project_object_id: str, bot_object_id: str, token: str) -> Bot:
    if not token:
        raise Http404()
    bot = Bot.objects.filter(
        project__object_id=project_object_id,
        object_id=bot_object_id,
        mom_guest_token=token,
    ).select_related("project").first()
    if not bot:
        raise Http404()
    return bot


def assert_guest_url_matches_session_type(bot: Bot, *, expect_app_session: bool) -> None:
    if expect_app_session and bot.session_type != SessionTypes.APP_SESSION:
        raise Http404()
    if not expect_app_session and bot.session_type != SessionTypes.BOT:
        raise Http404()


def authenticated_summary_api_urls(project_object_id: str, bot_object_id: str) -> dict[str, str]:
    return {
        "stream": reverse(
            "projects:stream-meeting-summary",
            kwargs={"object_id": project_object_id, "bot_object_id": bot_object_id},
        ),
        "save": reverse(
            "projects:save-meeting-summary",
            kwargs={"object_id": project_object_id, "bot_object_id": bot_object_id},
        ),
        "pdf": reverse(
            "projects:download-meeting-summary-pdf",
            kwargs={"object_id": project_object_id, "bot_object_id": bot_object_id},
        ),
        "docx": reverse(
            "projects:download-meeting-summary-docx",
            kwargs={"object_id": project_object_id, "bot_object_id": bot_object_id},
        ),
    }


def guest_summary_api_urls(
    project_object_id: str,
    bot_object_id: str,
    mom_guest_token: str,
    *,
    expect_app_session: bool,
) -> dict[str, str]:
    if expect_app_session:
        p = "guest-app-session-mom"
    else:
        p = "guest-bot-mom"
    kw = {"object_id": project_object_id, "bot_object_id": bot_object_id, "mom_guest_token": mom_guest_token}
    return {
        "stream": reverse(f"projects:{p}-summary-stream", kwargs=kw),
        "save": reverse(f"projects:{p}-summary-save", kwargs=kw),
        "pdf": reverse(f"projects:{p}-summary-pdf", kwargs=kw),
        "docx": reverse(f"projects:{p}-summary-docx", kwargs=kw),
    }
