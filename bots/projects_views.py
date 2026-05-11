import base64
import calendar
import json
import logging
import os
import uuid
from datetime import datetime
from urllib.parse import urlencode

import stripe
from allauth.account.utils import send_email_confirmation
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import models, transaction
from django.http import HttpResponse, JsonResponse, QueryDict, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views import View
from django.views.generic import ListView

from accounts.models import User, UserRole, user_can_manage_sensitive_integrations

from .bots_api_utils import BotCreationSource, create_bot, create_webhook_subscription, guest_mom_page_absolute_url
from .google_calendar_oauth import (
    GoogleCalendarOAuthError,
    build_google_calendar_oauth_authorize_url,
    connect_google_calendar,
    get_project_object_id_from_state,
    google_calendar_oauth_is_configured,
)
from .launch_bot_utils import launch_bot
from .meeting_url_utils import meeting_type_from_url
from .tasks.launch_scheduled_bot_task import launch_scheduled_bot
from .meeting_summary_guest_utils import (
    assert_guest_url_matches_session_type,
    authenticated_summary_api_urls,
    get_bot_for_guest_mom,
    guest_summary_api_urls,
    user_can_share_guest_mom_link,
)
from .meeting_summary_utils import (
    MeetingSummaryError,
    build_meeting_summary_docx,
    ensure_meeting_summary_pdf,
    generate_meeting_summary,
    generate_meeting_summary_stream,
    get_meeting_summary_availability_message,
    meeting_summary_is_ready,
    save_meeting_summary_artifacts,
    show_meeting_summary_panel,
)
from .models import (
    ApiKey,
    Bot,
    BotEvent,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    Calendar,
    CalendarEvent,
    CalendarPlatform,
    CalendarStates,
    ChatMessage,
    Credentials,
    CreditTransaction,
    GoogleMeetBotLogin,
    GoogleMeetBotLoginGroup,
    MeetingTypes,
    Participant,
    ParticipantEventTypes,
    Project,
    ProjectAccess,
    Recording,
    RecordingStates,
    RecordingTranscriptionStates,
    RecordingTypes,
    SessionTypes,
    Utterance,
    WebhookDeliveryAttempt,
    WebhookDeliveryAttemptStatus,
    WebhookSecret,
    WebhookSubscription,
    WebhookTriggerTypes,
    ZoomOAuthApp,
    ZoomOAuthConnection,
    ZoomOAuthConnectionStates,
)
from .serializers import DEFAULT_BOT_NAME
from .storage import remote_storage_url
from .stripe_utils import credit_amount_for_purchase_amount_dollars, process_checkout_session_completed
from .tasks.deliver_webhook_task import deliver_webhook
from .utils import generate_recordings_json_for_bot_detail_view
from .zoom_oauth import (
    ZoomOAuthError,
    build_zoom_oauth_authorize_url,
    connect_zoom_oauth_connection,
    get_zoom_oauth_state_data,
    zoom_oauth_redirect_uri,
)
from .zoom_oauth_apps_api_utils import create_or_update_zoom_oauth_app

logger = logging.getLogger(__name__)

GUEST_SESSION_CONCURRENT_BOTS_LIMIT = 3
GUEST_SESSION_MEETINGS_PAGE_SIZE = 6
GUEST_SESSION_LIMIT_ERROR = (
    "Boga Assistant cannot join right now because there are already 3 concurrent guest sessions. "
    "Sign in to use your account limit or try again when one session ends."
)


def _recordings_partial_context(bot: Bot) -> dict:
    """Shared context for the recordings + transcript + video partial (authenticated or guest)."""
    bot = (
        Bot.objects.select_related()
        .prefetch_related(
            models.Prefetch(
                "recordings",
                queryset=Recording.objects.prefetch_related(
                    models.Prefetch(
                        "utterances",
                        queryset=Utterance.objects.select_related("participant"),
                    ),
                ),
            ),
        )
        .get(pk=bot.pk)
    )
    return {
        "bot": bot,
        "BotStates": BotStates,
        "RecordingStates": RecordingStates,
        "RecordingTypes": RecordingTypes,
        "RecordingTranscriptionStates": RecordingTranscriptionStates,
        "recordings": generate_recordings_json_for_bot_detail_view(bot),
    }


def get_project_for_user(user, project_object_id):
    project = get_object_or_404(Project, object_id=project_object_id, organization=user.organization)
    # If you're an admin you can access any project in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=project, user=user).exists():
        raise PermissionDenied
    return project


def get_webhook_subscription_for_user(user, webhook_subscription_object_id):
    webhook_subscription = get_object_or_404(WebhookSubscription, object_id=webhook_subscription_object_id, project__organization=user.organization)
    # If you're an admin you can access any webhook subscription in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=webhook_subscription.project, user=user).exists():
        raise PermissionDenied
    return webhook_subscription


def get_api_key_for_user(user, api_key_object_id):
    api_key = get_object_or_404(ApiKey, object_id=api_key_object_id, project__organization=user.organization)
    # If you're an admin you can access any api key in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=api_key.project, user=user).exists():
        raise PermissionDenied
    return api_key


def get_calendar_for_user(user, calendar_object_id):
    calendar = get_object_or_404(Calendar, object_id=calendar_object_id, project__organization=user.organization)
    # If you're an admin you can access any calendar in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=calendar.project, user=user).exists():
        raise PermissionDenied
    return calendar


def get_calendar_event_for_user(user, calendar_event_object_id):
    calendar_event = get_object_or_404(CalendarEvent, object_id=calendar_event_object_id, calendar__project__organization=user.organization)
    # If you're an admin you can access any calendar event in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=calendar_event.calendar.project, user=user).exists():
        raise PermissionDenied
    return calendar_event


def get_google_meet_bot_login_for_user(user, google_meet_bot_login_object_id):
    google_meet_bot_login = get_object_or_404(GoogleMeetBotLogin, object_id=google_meet_bot_login_object_id, group__project__organization=user.organization)
    # If you're an admin you can access any Google Meet bot login in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=google_meet_bot_login.group.project, user=user).exists():
        raise PermissionDenied
    return google_meet_bot_login


def get_webhook_delivery_attempt_for_user(user, idempotency_key):
    webhook_delivery_attempt = get_object_or_404(WebhookDeliveryAttempt, idempotency_key=idempotency_key, webhook_subscription__project__organization=user.organization)
    # If you're an admin you can access any webhook delivery attempt in the organization
    if user.role != UserRole.ADMIN and not ProjectAccess.objects.filter(project=webhook_delivery_attempt.webhook_subscription.project, user=user).exists():
        raise PermissionDenied
    return webhook_delivery_attempt


def get_webhook_options_for_project(project):
    trigger_types = [trigger_type for trigger_type in WebhookTriggerTypes]
    if not project.organization.is_managed_zoom_oauth_enabled:
        trigger_types.remove(WebhookTriggerTypes.ZOOM_OAUTH_CONNECTION_STATE_CHANGE)
    if not project.organization.is_async_transcription_enabled:
        trigger_types.remove(WebhookTriggerTypes.ASYNC_TRANSCRIPTION_STATE_CHANGE)
    return trigger_types


def project_session_detail_url(project_object_id, bot):
    route_name = "bots:project-app-session-detail" if bot.session_type == SessionTypes.APP_SESSION else "bots:project-bot-detail"
    return reverse(route_name, kwargs={"object_id": project_object_id, "bot_object_id": bot.object_id})


def _attach_calendar_event_session_links(calendar_events, project_object_id):
    for calendar_event in calendar_events:
        linked_bot = next(iter(calendar_event.bots.all()), None)
        calendar_event.linked_session_bot = linked_bot
        calendar_event.session_detail_url = project_session_detail_url(project_object_id, linked_bot) if linked_bot else None


def _attach_bot_session_links(bots, project_object_id):
    for bot in bots:
        bot.session_detail_url = project_session_detail_url(project_object_id, bot)


def get_partial_for_credential_type(credential_type, request, context):
    if credential_type == Credentials.CredentialTypes.ZOOM_OAUTH:
        return render(request, "projects/partials/zoom_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.DEEPGRAM:
        return render(request, "projects/partials/deepgram_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.GLADIA:
        return render(request, "projects/partials/gladia_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.OPENAI:
        return render(request, "projects/partials/openai_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.GOOGLE_TTS:
        return render(request, "projects/partials/google_tts_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.ASSEMBLY_AI:
        return render(request, "projects/partials/assembly_ai_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.SARVAM:
        return render(request, "projects/partials/sarvam_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.ELEVENLABS:
        return render(request, "projects/partials/elevenlabs_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.TEAMS_BOT_LOGIN:
        return render(request, "projects/partials/teams_bot_login_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.KYUTAI:
        return render(request, "projects/partials/kyutai_credentials.html", context)
    elif credential_type == Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE:
        return render(request, "projects/partials/external_media_storage_credentials.html", context)
    else:
        return HttpResponse("Cannot render the partial for this credential type", status=400)


class AdminRequiredMixin(LoginRequiredMixin):
    """
    Mixin for class-based views that can only be accessed by admin users.
    Inherits from LoginRequiredMixin to ensure user is authenticated first.
    """

    def dispatch(self, request, *args, **kwargs):
        # First check if user is authenticated (handled by LoginRequiredMixin)
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        # Then check if user is admin
        if request.user.role != UserRole.ADMIN:
            raise PermissionDenied("Only administrators can access this resource.")

        return super().dispatch(request, *args, **kwargs)


class MeetingCreatorSensitiveIntegrationsDeniedMixin(LoginRequiredMixin):
    """Block meeting_creator from credentials, webhooks, calendars, and related Google Meet / Zoom app settings."""

    meeting_creator_denied_message = (
        "Meeting creator accounts can create bots and app sessions, but cannot change credentials, "
        "webhooks, or calendars. Ask an administrator."
    )

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and getattr(request.user, "role", None) == UserRole.MEETING_CREATOR:
            raise PermissionDenied(self.meeting_creator_denied_message)
        return super().dispatch(request, *args, **kwargs)


def _resolve_user_role_from_post(request) -> str:
    """Invite/edit user forms: prefer user_role; fall back to legacy is_admin checkbox."""
    raw = request.POST.get("user_role")
    if raw in UserRole.values:
        return raw
    if request.POST.get("is_admin") == "true":
        return UserRole.ADMIN
    return UserRole.REGULAR_USER


class ProjectUrlContextMixin:
    def get_project_context(self, object_id, project):
        return {
            "project": project,
            "charge_credits_for_bots_setting": settings.CHARGE_CREDITS_FOR_BOTS,
            "user_projects": Project.accessible_to(self.request.user),
            "UserRole": UserRole,
            "user_can_manage_sensitive_integrations": user_can_manage_sensitive_integrations(self.request.user),
            "debug_mode": True if settings.DEBUG else False,
            "zoom_oauth_redirect_uri": zoom_oauth_redirect_uri(project),
        }


class ProjectDashboardView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        try:
            project = get_project_for_user(user=request.user, project_object_id=object_id)
        except:
            return redirect("/")

        # Quick start guide status checks
        zoom_credentials = project.zoom_oauth_apps.exists() or Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH).exists()

        deepgram_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.DEEPGRAM).exists()

        has_api_keys = ApiKey.objects.filter(project=project).exists()

        has_ended_bots = Bot.objects.filter(project=project, state=BotStates.ENDED).exists()

        has_created_bots_via_api = BotEvent.objects.filter(bot__project=project, event_type=BotEventTypes.JOIN_REQUESTED, metadata__source=BotCreationSource.API).exists()

        context = self.get_project_context(object_id, project)
        context.update(
            {
                "quick_start": {
                    "has_credentials": zoom_credentials and deepgram_credentials,
                    "has_api_keys": has_api_keys,
                    "has_ended_bots": has_ended_bots,
                    "has_created_bots_via_api": has_created_bots_via_api,
                },
            }
        )

        return render(request, "projects/project_dashboard.html", context)


class ProjectApiKeysView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        context = self.get_project_context(object_id, project)
        context["api_keys"] = ApiKey.objects.filter(project=project).order_by("-created_at")
        return render(request, "projects/project_api_keys.html", context)


class CreateApiKeyView(LoginRequiredMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        name = request.POST.get("name")

        if not name:
            return HttpResponse("Name is required", status=400)

        api_key_instance, api_key = ApiKey.create(project=project, name=name)

        # Render the success modal content
        return render(
            request,
            "projects/partials/api_key_created_modal.html",
            {"api_key": api_key, "name": name},
        )


class DeleteApiKeyView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def delete(self, request, object_id, key_object_id):
        api_key = get_api_key_for_user(user=request.user, api_key_object_id=key_object_id)
        api_key.delete()
        context = self.get_project_context(object_id, api_key.project)
        context["api_keys"] = ApiKey.objects.filter(project=api_key.project).order_by("-created_at")
        return render(request, "projects/project_api_keys.html", context)


class RedirectToDashboardView(LoginRequiredMixin, View):
    def get(self, request, object_id, extra=None):
        return redirect("bots:project-dashboard", object_id=object_id)


class DeleteZoomOAuthAppView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        zoom_oauth_app = ZoomOAuthApp.objects.filter(project=project).first()
        if not zoom_oauth_app:
            return HttpResponse("Zoom OAuth app not found", status=404)
        zoom_oauth_app.delete()
        context = self.get_project_context(object_id, project)
        return render(request, "projects/partials/zoom_oauth_app.html", context)


class CreateZoomOAuthAppView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        zoom_oauth_app, error = create_or_update_zoom_oauth_app(
            project=project,
            client_id=request.POST.get("client_id"),
            client_secret=request.POST.get("client_secret"),
            webhook_secret=request.POST.get("webhook_secret"),
        )

        if error:
            return HttpResponse(error, status=400)

        context = self.get_project_context(object_id, project)
        context["zoom_oauth_app"] = zoom_oauth_app
        return render(request, "projects/partials/zoom_oauth_app.html", context)


class StartGoogleCalendarOAuthView(MeetingCreatorSensitiveIntegrationsDeniedMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        if not google_calendar_oauth_is_configured():
            return redirect(
                f"{reverse('projects:project-calendars', kwargs={'object_id': project.object_id})}?{urlencode({'google_calendar_error': 'Google Calendar OAuth is not configured yet. Set GOOGLE_CALENDAR_OAUTH_CLIENT_ID and GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET first.'})}"
            )

        return redirect(build_google_calendar_oauth_authorize_url(project))


class GoogleCalendarOAuthCallbackView(MeetingCreatorSensitiveIntegrationsDeniedMixin, View):
    def get(self, request):
        state = request.GET.get("state")
        if not state:
            return HttpResponse("Missing Google Calendar OAuth state.", status=400)

        try:
            project_object_id = get_project_object_id_from_state(state)
            project = get_project_for_user(user=request.user, project_object_id=project_object_id)
        except GoogleCalendarOAuthError as exc:
            return HttpResponse(str(exc), status=400)

        calendars_url = reverse("projects:project-calendars", kwargs={"object_id": project.object_id})

        google_error = request.GET.get("error")
        if google_error:
            error_description = request.GET.get("error_description") or google_error.replace("_", " ")
            return redirect(f"{calendars_url}?{urlencode({'google_calendar_error': error_description})}")

        authorization_code = request.GET.get("code")
        if not authorization_code:
            return redirect(f"{calendars_url}?{urlencode({'google_calendar_error': 'Google did not return an authorization code.'})}")

        try:
            calendar = connect_google_calendar(project=project, authorization_code=authorization_code)
        except GoogleCalendarOAuthError as exc:
            return redirect(f"{calendars_url}?{urlencode({'google_calendar_error': str(exc)})}")

        success_message = f"Connected Google Calendar for {calendar.deduplication_key}."
        return redirect(f"{calendars_url}?{urlencode({'google_calendar_success': success_message})}")


class StartZoomOAuthView(MeetingCreatorSensitiveIntegrationsDeniedMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        credentials_url = reverse("projects:project-credentials", kwargs={"object_id": project.object_id})

        try:
            return redirect(build_zoom_oauth_authorize_url(project))
        except ZoomOAuthError as exc:
            return redirect(f"{credentials_url}?{urlencode({'zoom_oauth_error': str(exc)})}")


class ZoomOAuthCallbackView(MeetingCreatorSensitiveIntegrationsDeniedMixin, View):
    def get(self, request, object_id=None):
        state = request.GET.get("state")
        try:
            if state:
                state_data = get_zoom_oauth_state_data(state)
                project_object_id = state_data["project_object_id"]
                if object_id and object_id != project_object_id:
                    return HttpResponse("Zoom OAuth state does not match the callback project.", status=400)
            elif object_id:
                project_object_id = object_id
                state_data = {}
            else:
                return HttpResponse("Missing Zoom OAuth state.", status=400)

            project = get_project_for_user(user=request.user, project_object_id=project_object_id)
        except ZoomOAuthError as exc:
            return HttpResponse(str(exc), status=400)

        credentials_url = reverse("projects:project-credentials", kwargs={"object_id": project.object_id})

        zoom_error = request.GET.get("error")
        if zoom_error:
            error_description = request.GET.get("error_description") or zoom_error.replace("_", " ")
            return redirect(f"{credentials_url}?{urlencode({'zoom_oauth_error': error_description})}")

        authorization_code = request.GET.get("code")
        if not authorization_code:
            return redirect(f"{credentials_url}?{urlencode({'zoom_oauth_error': 'Zoom did not return an authorization code.'})}")

        try:
            zoom_oauth_connection = connect_zoom_oauth_connection(
                project=project,
                authorization_code=authorization_code,
                state_data=state_data,
            )
        except ZoomOAuthError as exc:
            return redirect(f"{credentials_url}?{urlencode({'zoom_oauth_error': str(exc)})}")

        success_message = f"Connected Zoom account {zoom_oauth_connection.user_id}."
        return redirect(f"{credentials_url}?{urlencode({'zoom_oauth_success': success_message})}")


class CreateCredentialsView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        try:
            credential_type = int(request.POST.get("credential_type"))
            if credential_type not in [choice[0] for choice in Credentials.CredentialTypes.choices]:
                return HttpResponse("Invalid credential type", status=400)

            # Get or create the credential instance
            credential, created = Credentials.objects.get_or_create(project=project, credential_type=credential_type)

            # Parse the credentials data based on type
            if credential_type == Credentials.CredentialTypes.ZOOM_OAUTH:
                credentials_data = {
                    "client_id": request.POST.get("client_id"),
                    "client_secret": request.POST.get("client_secret"),
                }

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)

            elif credential_type == Credentials.CredentialTypes.DEEPGRAM:
                credentials_data = {"api_key": request.POST.get("api_key")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.GLADIA:
                credentials_data = {"api_key": request.POST.get("api_key")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.OPENAI:
                credentials_data = {"api_key": request.POST.get("api_key")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.ASSEMBLY_AI:
                credentials_data = {"api_key": request.POST.get("api_key")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.SARVAM:
                credentials_data = {"api_key": request.POST.get("api_key")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.ELEVENLABS:
                credentials_data = {"api_key": request.POST.get("api_key")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.KYUTAI:
                credentials_data = {
                    "server_url": request.POST.get("server_url"),
                }
                # Only include api_key if it's provided
                api_key = request.POST.get("api_key")
                if api_key:
                    credentials_data["api_key"] = api_key

                if not credentials_data.get("server_url"):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.GOOGLE_TTS:
                credentials_data = {"service_account_json": request.POST.get("service_account_json")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.TEAMS_BOT_LOGIN:
                credentials_data = {"username": request.POST.get("username"), "password": request.POST.get("password")}

                if not all(credentials_data.values()):
                    return HttpResponse("Missing required credentials data", status=400)
            elif credential_type == Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE:
                credentials_data = {"access_key_id": request.POST.get("access_key_id"), "access_key_secret": request.POST.get("access_key_secret"), "endpoint_url": request.POST.get("endpoint_url"), "region_name": request.POST.get("region_name")}

                if not credentials_data.get("access_key_id") or not credentials_data.get("access_key_secret") or (not credentials_data.get("endpoint_url") and not credentials_data.get("region_name")):
                    return HttpResponse("Missing required credentials data", status=400)
            else:
                return HttpResponse("Unsupported credential type", status=400)

            # Store the encrypted credentials
            credential.set_credentials(credentials_data)

            # Return the entire settings page with updated context
            context = self.get_project_context(object_id, project)
            context["credentials"] = credential.get_credentials()
            context["credential_type"] = credential.credential_type

            # Render the appropriate partial based on credential type
            return get_partial_for_credential_type(credential.credential_type, request, context)

        except Exception as e:
            return HttpResponse(str(e), status=400)


class DeleteCredentialsView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        try:
            credential_type = int(request.POST.get("credential_type"))
            if credential_type not in [choice[0] for choice in Credentials.CredentialTypes.choices]:
                return HttpResponse("Invalid credential type", status=400)

            # Find and delete the credential
            credential = Credentials.objects.filter(project=project, credential_type=credential_type).first()

            if credential:
                credential.delete()

            # Return the updated partial for the specific credential type
            context = self.get_project_context(object_id, project)
            context["credentials"] = None
            context["credential_type"] = credential_type

            # Render the appropriate partial based on credential type
            return get_partial_for_credential_type(credential_type, request, context)

        except Exception as e:
            error_id = str(uuid.uuid4())
            logger.error(f"Error deleting credentials (error_id={error_id}): {e}")
            return HttpResponse(f"Error deleting credentials. Error ID: {error_id}", status=400)


class ProjectCredentialsView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        # Try to get existing zoom oauth app
        zoom_oauth_app = ZoomOAuthApp.objects.filter(project=project).first()

        # Try to get existing google meet bot login group
        google_meet_bot_login_group = GoogleMeetBotLoginGroup.objects.filter(project=project).first()

        # Try to get existing credentials
        zoom_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH).first()

        deepgram_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.DEEPGRAM).first()

        gladia_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.GLADIA).first()

        openai_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.OPENAI).first()

        google_tts_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.GOOGLE_TTS).first()

        assembly_ai_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.ASSEMBLY_AI).first()

        sarvam_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.SARVAM).first()

        elevenlabs_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.ELEVENLABS).first()

        kyutai_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.KYUTAI).first()

        teams_bot_login_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.TEAMS_BOT_LOGIN).first()

        external_media_storage_credentials = Credentials.objects.filter(project=project, credential_type=Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE).first()

        context = self.get_project_context(object_id, project)
        context.update(
            {
                "zoom_oauth_app": zoom_oauth_app,
                "zoom_oauth_redirect_uri": zoom_oauth_redirect_uri(project),
                "zoom_oauth_success": request.GET.get("zoom_oauth_success"),
                "zoom_oauth_error": request.GET.get("zoom_oauth_error"),
                "google_meet_bot_login_group": google_meet_bot_login_group,
                "zoom_credentials": zoom_credentials.get_credentials() if zoom_credentials else None,
                "zoom_credential_type": Credentials.CredentialTypes.ZOOM_OAUTH,
                "deepgram_credentials": deepgram_credentials.get_credentials() if deepgram_credentials else None,
                "deepgram_credential_type": Credentials.CredentialTypes.DEEPGRAM,
                "google_tts_credentials": google_tts_credentials.get_credentials() if google_tts_credentials else None,
                "google_tts_credential_type": Credentials.CredentialTypes.GOOGLE_TTS,
                "gladia_credentials": gladia_credentials.get_credentials() if gladia_credentials else None,
                "gladia_credential_type": Credentials.CredentialTypes.GLADIA,
                "openai_credentials": openai_credentials.get_credentials() if openai_credentials else None,
                "openai_credential_type": Credentials.CredentialTypes.OPENAI,
                "assembly_ai_credentials": assembly_ai_credentials.get_credentials() if assembly_ai_credentials else None,
                "assembly_ai_credential_type": Credentials.CredentialTypes.ASSEMBLY_AI,
                "sarvam_credentials": sarvam_credentials.get_credentials() if sarvam_credentials else None,
                "sarvam_credential_type": Credentials.CredentialTypes.SARVAM,
                "elevenlabs_credentials": elevenlabs_credentials.get_credentials() if elevenlabs_credentials else None,
                "elevenlabs_credential_type": Credentials.CredentialTypes.ELEVENLABS,
                "kyutai_credentials": kyutai_credentials.get_credentials() if kyutai_credentials else None,
                "kyutai_credential_type": Credentials.CredentialTypes.KYUTAI,
                "teams_bot_login_credentials": teams_bot_login_credentials.get_credentials() if teams_bot_login_credentials else None,
                "teams_bot_login_credential_type": Credentials.CredentialTypes.TEAMS_BOT_LOGIN,
                "external_media_storage_credentials": external_media_storage_credentials.get_credentials() if external_media_storage_credentials else None,
                "external_media_storage_credential_type": Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE,
            }
        )

        return render(request, "projects/project_credentials.html", context)


class ProjectBotsView(LoginRequiredMixin, ProjectUrlContextMixin, ListView):
    template_name = "projects/project_bots.html"
    context_object_name = "bots"
    paginate_by = 20
    session_type = None

    def get_session_type(self):
        """Get session type from class attribute"""
        return self.session_type

    def get_queryset(self):
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])

        # Filter based on session type
        queryset = Bot.objects.filter(project=project, session_type=self.get_session_type())

        # Apply date filters if provided
        start_date = self.request.GET.get("start_date")
        end_date = self.request.GET.get("end_date")

        if start_date:
            queryset = queryset.filter(created_at__gte=start_date)
        if end_date:
            # Add 1 day to include the end date fully
            from datetime import datetime, timedelta

            try:
                end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")
                end_date_obj = end_date_obj + timedelta(days=1)
                queryset = queryset.filter(created_at__lt=end_date_obj)
            except (ValueError, TypeError):
                # Handle invalid date format
                pass

        # Apply join_at date filters if provided
        join_at_start = self.request.GET.get("join_at_start")
        join_at_end = self.request.GET.get("join_at_end")

        if join_at_start:
            queryset = queryset.filter(join_at__gte=join_at_start)
        if join_at_end:
            from datetime import datetime, timedelta

            try:
                join_at_end_obj = datetime.strptime(join_at_end, "%Y-%m-%d")
                join_at_end_obj = join_at_end_obj + timedelta(days=1)
                queryset = queryset.filter(join_at__lt=join_at_end_obj)
            except (ValueError, TypeError):
                # Handle invalid date format
                pass

        # Apply state filters if provided
        states = self.request.GET.getlist("states")
        if states:
            # Convert string values to integers
            try:
                state_values = [int(state) for state in states if state.isdigit()]
                if state_values:
                    queryset = queryset.filter(state__in=state_values)
            except (ValueError, TypeError):
                # Handle invalid state values
                pass

        # Apply search filter if provided
        search_query = self.request.GET.get("search", "").strip()
        if search_query:
            queryset = queryset.filter(models.Q(object_id__icontains=search_query) | models.Q(meeting_url__icontains=search_query) | models.Q(name__icontains=search_query))

        # Apply ended_at date filters if provided
        ended_at_start = self.request.GET.get("ended_at_start")
        ended_at_end = self.request.GET.get("ended_at_end")

        if ended_at_start or ended_at_end:
            ended_at_filters = {"bot_events__new_state__in": [BotStates.ENDED, BotStates.FATAL_ERROR]}
            if ended_at_start:
                ended_at_filters["bot_events__created_at__gte"] = ended_at_start
            if ended_at_end:
                from datetime import datetime, timedelta

                try:
                    ended_at_end_obj = datetime.strptime(ended_at_end, "%Y-%m-%d")
                    ended_at_end_obj = ended_at_end_obj + timedelta(days=1)
                    ended_at_filters["bot_events__created_at__lt"] = ended_at_end_obj
                except (ValueError, TypeError):
                    pass
            queryset = queryset.filter(**ended_at_filters).distinct()

        # Apply joined meeting filter if provided
        joined_meeting = self.request.GET.get("joined_meeting", "").strip()
        if joined_meeting == "yes":
            queryset = queryset.filter(bot_events__event_type=BotEventTypes.BOT_JOINED_MEETING).distinct()
        elif joined_meeting == "no":
            queryset = queryset.exclude(bot_events__event_type=BotEventTypes.BOT_JOINED_MEETING)

        # Apply unexpected error filter if provided
        unexpected_error = self.request.GET.get("unexpected_error", "").strip()
        if unexpected_error == "yes":
            queryset = queryset.filter(bot_events__event_type=BotEventTypes.FATAL_ERROR).distinct()
        elif unexpected_error == "no":
            queryset = queryset.exclude(bot_events__event_type=BotEventTypes.FATAL_ERROR)

        # Get the latest bot event type and subtype for each bot using subquery annotations
        latest_event_subquery_base = BotEvent.objects.filter(bot=models.OuterRef("pk")).order_by("-created_at")
        latest_event_type = latest_event_subquery_base.values("event_type")[:1]
        latest_event_sub_type = latest_event_subquery_base.values("event_sub_type")[:1]

        # Apply annotations and ordering
        queryset = queryset.annotate(last_event_type=models.Subquery(latest_event_type), last_event_sub_type=models.Subquery(latest_event_sub_type)).order_by("-created_at")

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])
        context.update(self.get_project_context(self.kwargs["object_id"], project))

        # Add BotStates and SessionTypes for the template
        context["BotStates"] = BotStates
        context["SessionTypes"] = SessionTypes

        # Add session type to context
        context["session_type"] = self.get_session_type()

        # Add filter parameters to context for maintaining state
        context["filter_params"] = {"start_date": self.request.GET.get("start_date", ""), "end_date": self.request.GET.get("end_date", ""), "join_at_start": self.request.GET.get("join_at_start", ""), "join_at_end": self.request.GET.get("join_at_end", ""), "ended_at_start": self.request.GET.get("ended_at_start", ""), "ended_at_end": self.request.GET.get("ended_at_end", ""), "states": self.request.GET.getlist("states"), "search": self.request.GET.get("search", ""), "joined_meeting": self.request.GET.get("joined_meeting", ""), "unexpected_error": self.request.GET.get("unexpected_error", "")}

        # Add flag to detect if create modal should be automatically opened
        context["open_create_modal"] = self.request.GET.get("open_create_modal") == "true"

        # Check if any bots in the current page have a join_at value
        context["has_scheduled_bots"] = any(bot.join_at is not None for bot in context["bots"])

        # Only iterates over the paginated page (<= 20)
        for bot in context["bots"]:
            if bot.last_event_type:
                bot.last_event_type_display = dict(BotEventTypes.choices).get(bot.last_event_type, str(bot.last_event_type))
            if bot.last_event_sub_type:
                bot.last_event_sub_type_display = dict(BotEventSubTypes.choices).get(bot.last_event_sub_type, str(bot.last_event_sub_type))
            bot.guest_mom_share_url = guest_mom_page_absolute_url(bot) if bot.mom_guest_token else None

        context["can_share_guest_mom_link"] = user_can_share_guest_mom_link(self.request.user, project)

        return context


class ProjectCalendarsView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, ListView):
    template_name = "projects/project_calendars.html"
    context_object_name = "calendars"
    paginate_by = 20

    def get_queryset(self):
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])

        # Start with the base queryset
        queryset = Calendar.objects.filter(project=project)

        # Apply date filters if provided
        start_date = self.request.GET.get("start_date")
        end_date = self.request.GET.get("end_date")

        if start_date:
            queryset = queryset.filter(created_at__gte=start_date)
        if end_date:
            # Add 1 day to include the end date fully
            from datetime import datetime, timedelta

            try:
                end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")
                end_date_obj = end_date_obj + timedelta(days=1)
                queryset = queryset.filter(created_at__lt=end_date_obj)
            except (ValueError, TypeError):
                # Handle invalid date format
                pass

        # Apply state filters if provided
        states = self.request.GET.getlist("states")
        if states:
            # Convert string values to integers
            try:
                state_values = [int(state) for state in states if state.isdigit()]
                if state_values:
                    queryset = queryset.filter(state__in=state_values)
            except (ValueError, TypeError):
                # Handle invalid state values
                pass

        # Apply deduplication key filter if provided
        deduplication_key = self.request.GET.get("deduplication_key")
        if deduplication_key:
            # Filter for calendars with specific deduplication key
            queryset = queryset.filter(deduplication_key__icontains=deduplication_key)

        # Order by most recently created
        queryset = queryset.order_by("-created_at")

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])
        public_site_domain = os.getenv("EXTERNAL_WEBHOOK_SITE_DOMAIN") or settings.SITE_DOMAIN
        context.update(self.get_project_context(self.kwargs["object_id"], project))

        # Add CalendarStates and CalendarPlatform for the template
        context["CalendarStates"] = CalendarStates
        context["CalendarPlatform"] = CalendarPlatform
        context["google_calendar_oauth_enabled"] = google_calendar_oauth_is_configured()
        context["google_calendar_success"] = self.request.GET.get("google_calendar_success")
        context["google_calendar_error"] = self.request.GET.get("google_calendar_error")
        context["google_calendar_public_domain"] = public_site_domain
        context["google_calendar_public_domain_is_local"] = public_site_domain.startswith("localhost")
        context["google_calendar_host_mismatch"] = public_site_domain != self.request.get_host()

        # Add filter parameters to context for maintaining state
        context["filter_params"] = {
            "start_date": self.request.GET.get("start_date", ""),
            "end_date": self.request.GET.get("end_date", ""),
            "states": self.request.GET.getlist("states"),
            "deduplication_key": self.request.GET.get("deduplication_key", ""),
        }

        return context


class ProjectCalendarDetailView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, ListView):
    template_name = "projects/project_calendar_detail.html"
    context_object_name = "calendar_events"
    paginate_by = 20

    def get_calendar(self):
        """Get the calendar object, cached for multiple calls"""
        if not hasattr(self, "_calendar"):
            try:
                self._calendar = get_calendar_for_user(user=self.request.user, calendar_object_id=self.kwargs["calendar_object_id"])
            except PermissionDenied:
                self._calendar = None
        return self._calendar

    def get_queryset(self):
        calendar = self.get_calendar()
        if not calendar:
            return []

        # Get calendar events for this calendar, ordered by start time (most recent first)
        return calendar.events.prefetch_related(models.Prefetch("bots", queryset=Bot.objects.order_by("-created_at"))).order_by("-start_time")

    def get(self, request, object_id, calendar_object_id):
        # Check if calendar exists, if not redirect
        calendar = self.get_calendar()
        if not calendar:
            return redirect("bots:project-calendars", object_id=object_id)

        # Check if project from url is the same as the calendar's project
        if calendar.project.object_id != object_id:
            return redirect("bots:project-calendars", object_id=object_id)

        # Continue with normal ListView processing
        return super().get(request, object_id, calendar_object_id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        calendar = self.get_calendar()
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])

        # Get webhook delivery attempts for this calendar (from calendar-related webhook subscriptions)
        webhook_delivery_attempts = WebhookDeliveryAttempt.objects.filter(calendar=calendar).select_related("webhook_subscription").order_by("-created_at")

        context.update(self.get_project_context(self.kwargs["object_id"], project))
        context.update(
            {
                "calendar": calendar,
                "CalendarStates": CalendarStates,
                "CalendarPlatform": CalendarPlatform,
                "webhook_delivery_attempts": webhook_delivery_attempts,
                "WebhookDeliveryAttemptStatus": WebhookDeliveryAttemptStatus,
            }
        )
        _attach_calendar_event_session_links(context["calendar_events"], self.kwargs["object_id"])

        return context


class ProjectCalendarEventDetailView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id, calendar_object_id, event_object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        calendar_event = get_calendar_event_for_user(user=request.user, calendar_event_object_id=event_object_id)

        # Verify the calendar event belongs to the specified calendar
        if calendar_event.calendar.object_id != calendar_object_id:
            return redirect("bots:project-calendar-detail", object_id=object_id, calendar_object_id=calendar_object_id)

        # Check if project from url is the same as the calendar's project
        if calendar_event.calendar.project.object_id != object_id:
            return redirect("bots:project-calendar-detail", object_id=object_id, calendar_object_id=calendar_object_id)

        # Get any bots that were created for this calendar event
        bots_for_event = list(Bot.objects.filter(calendar_event=calendar_event).order_by("-created_at"))
        _attach_bot_session_links(bots_for_event, object_id)

        context = self.get_project_context(object_id, project)
        context.update(
            {
                "calendar": calendar_event.calendar,
                "calendar_event": calendar_event,
                "bots_for_event": bots_for_event,
                "primary_session_bot": bots_for_event[0] if bots_for_event else None,
                "CalendarStates": CalendarStates,
                "CalendarPlatform": CalendarPlatform,
                "BotStates": BotStates,
            }
        )

        return render(request, "projects/project_calendar_event_detail.html", context)


class ProjectBotDetailView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id, bot_object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        try:
            bot = (
                Bot.objects.select_related()
                .prefetch_related(
                    "bot_events__debug_screenshots",
                )
                .get(object_id=bot_object_id, project=project)
            )
        except Bot.DoesNotExist:
            # Redirect to bots list if bot not found
            return redirect("bots:project-bots", object_id=object_id)

        # Get webhook delivery attempts for this bot (from both project-level and bot-specific webhook subscriptions)
        webhook_delivery_attempts = WebhookDeliveryAttempt.objects.filter(bot=bot).select_related("webhook_subscription").order_by("-created_at")

        # Get chat messages for this bot
        chat_messages = ChatMessage.objects.filter(bot=bot).select_related("participant").order_by("created_at")

        # Get participants and participant events for this bot
        participants = Participant.objects.filter(bot=bot, is_the_bot=False).prefetch_related("events").order_by("created_at")

        # Get resource snapshots for this bot
        resource_snapshots = bot.resource_snapshots.all().order_by("created_at")

        # Calculate maximum values from resource snapshots
        max_ram_usage = 0
        max_cpu_usage = 0
        max_db_connection_count = 0
        max_redis_connection_count = 0
        network_stats = None
        public_ip = None
        if resource_snapshots.exists():
            for snapshot in resource_snapshots:
                data = snapshot.data
                if public_ip is None:
                    public_ip = data.get("public_ip")
                ram_usage = data.get("ram_usage_megabytes") or 0
                cpu_usage = data.get("cpu_usage_millicores") or 0
                db_connection_count = data.get("db_connection_count") or 0
                redis_connection_count = data.get("redis_connection_count") or 0

                if ram_usage > max_ram_usage:
                    max_ram_usage = ram_usage
                if cpu_usage > max_cpu_usage:
                    max_cpu_usage = cpu_usage
                if db_connection_count > max_db_connection_count:
                    max_db_connection_count = db_connection_count
                if redis_connection_count > max_redis_connection_count:
                    max_redis_connection_count = redis_connection_count

                network = data.get("network")
                if network:
                    if network_stats is None:
                        network_stats = {
                            "max_rx_bytes_per_sec": 0,
                            "max_tx_bytes_per_sec": 0,
                            "max_rx_packets_per_sec": 0,
                            "max_tx_packets_per_sec": 0,
                            "total_rx_dropped": 0,
                            "total_tx_dropped": 0,
                            "total_rx_errors": 0,
                            "total_tx_errors": 0,
                        }
                    network_stats["max_rx_bytes_per_sec"] = max(network_stats["max_rx_bytes_per_sec"], network.get("rx_bytes_per_sec") or 0)
                    network_stats["max_tx_bytes_per_sec"] = max(network_stats["max_tx_bytes_per_sec"], network.get("tx_bytes_per_sec") or 0)
                    network_stats["max_rx_packets_per_sec"] = max(network_stats["max_rx_packets_per_sec"], network.get("rx_packets_per_sec") or 0)
                    network_stats["max_tx_packets_per_sec"] = max(network_stats["max_tx_packets_per_sec"], network.get("tx_packets_per_sec") or 0)
                    network_stats["total_rx_dropped"] += network.get("rx_dropped_delta") or 0
                    network_stats["total_tx_dropped"] += network.get("tx_dropped_delta") or 0
                    network_stats["total_rx_errors"] += network.get("rx_errors_delta") or 0
                    network_stats["total_tx_errors"] += network.get("tx_errors_delta") or 0

        meeting_summary_status_message = get_meeting_summary_availability_message(bot)

        context = self.get_project_context(object_id, project)
        context.update(
            {
                "bot": bot,
                "BotStates": BotStates,
                "SessionTypes": SessionTypes,
                "can_share_guest_mom_link": user_can_share_guest_mom_link(request.user, project),
                "guest_mom_share_url": guest_mom_page_absolute_url(bot) if bot.mom_guest_token else None,
                "summary_api_urls": authenticated_summary_api_urls(project.object_id, bot.object_id),
                "meeting_summary_ready": meeting_summary_is_ready(bot),
                "meeting_summary_status_message": meeting_summary_status_message,
                "meeting_summary": bot.meeting_summary or "",
                "webhook_delivery_attempts": webhook_delivery_attempts,
                "chat_messages": chat_messages,
                "participants": participants,
                "ParticipantEventTypes": ParticipantEventTypes,
                "WebhookDeliveryAttemptStatus": WebhookDeliveryAttemptStatus,
                "credits_consumed": -sum([t.credits_delta() for t in bot.credit_transactions.all()]) if bot.credit_transactions.exists() else None,
                "resource_snapshots": resource_snapshots,
                "max_ram_usage": max_ram_usage,
                "max_cpu_usage": max_cpu_usage,
                "max_db_connection_count": max_db_connection_count,
                "max_redis_connection_count": max_redis_connection_count,
                "network_stats": network_stats,
                "public_ip": public_ip,
                "show_meeting_summary_panel": show_meeting_summary_panel(bot),
                "can_manual_complete_session": BotEventManager.can_manual_complete_session(bot),
            }
        )

        return render(request, "projects/project_bot_detail.html", context)


class GenerateMeetingSummaryView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id, bot_object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        bot = get_object_or_404(Bot, object_id=bot_object_id, project=project)

        meeting_summary = None
        meeting_summary_error = None
        meeting_summary_status_message = get_meeting_summary_availability_message(bot)

        try:
            meeting_summary = generate_meeting_summary(bot)
            save_meeting_summary_artifacts(bot, meeting_summary)
        except MeetingSummaryError as exc:
            meeting_summary_error = str(exc)

        context = {
            "project": project,
            "bot": bot,
            "meeting_summary": meeting_summary,
            "meeting_summary_error": meeting_summary_error,
            "meeting_summary_ready": meeting_summary is not None or meeting_summary_is_ready(bot),
            "meeting_summary_status_message": meeting_summary_status_message,
            "summary_api_urls": authenticated_summary_api_urls(project.object_id, bot.object_id),
            "can_share_guest_mom_link": user_can_share_guest_mom_link(request.user, project),
            "guest_mom_share_url": guest_mom_page_absolute_url(bot) if bot.mom_guest_token else None,
        }
        return render(request, "projects/partials/project_bot_summary.html", context)


class ManualCompleteBotSessionView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    """Let a logged-in project user mark a stuck bot session as ended (dashboard recovery)."""

    def post(self, request, object_id, bot_object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        bot = get_object_or_404(Bot, object_id=bot_object_id, project=project)

        def _detail_redirect():
            name = (
                "projects:project-app-session-detail"
                if bot.session_type == SessionTypes.APP_SESSION
                else "projects:project-bot-detail"
            )
            return redirect(name, object_id=object_id, bot_object_id=bot_object_id)

        if not BotEventManager.can_manual_complete_session(bot):
            messages.error(request, "This session cannot be manually completed in its current state.")
            return _detail_redirect()
        try:
            BotEventManager.manual_complete_session_for_stuck_bot(bot, resolved_by_user_id=request.user.id)
        except ValidationError as exc:
            err_text = str(exc)
            if getattr(exc, "messages", None):
                err_text = exc.messages[0]
            messages.error(request, err_text)
            return _detail_redirect()
        messages.success(
            request,
            "Session marked as completed. You can generate MoM from the transcript when it is available.",
        )
        bot.refresh_from_db()
        return _detail_redirect()


class StreamMeetingSummaryView(LoginRequiredMixin, View):
    def post(self, request, object_id, bot_object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        bot = get_object_or_404(Bot, object_id=bot_object_id, project=project)

        try:
            stream = generate_meeting_summary_stream(bot)
        except MeetingSummaryError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        def event_stream():
            collected = []
            try:
                for chunk in stream:
                    collected.append(chunk)
                    yield f"data: {json.dumps({'delta': chunk})}\n\n"
            except MeetingSummaryError as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            except Exception:
                logger.exception("Unexpected error during meeting summary streaming for bot %s", bot.object_id)
                yield f"data: {json.dumps({'error': 'An unexpected error occurred. Please try again.'})}\n\n"
            finally:
                full_text = "".join(collected).strip()
                if full_text:
                    save_meeting_summary_artifacts(bot, full_text)
                yield "data: [DONE]\n\n"

        response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


class SaveMeetingSummaryView(LoginRequiredMixin, View):
    def post(self, request, object_id, bot_object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        bot = get_object_or_404(Bot, object_id=bot_object_id, project=project)

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        markdown_content = data.get("markdown", "").strip()
        if not markdown_content:
            return JsonResponse({"error": "Summary content cannot be empty"}, status=400)

        save_meeting_summary_artifacts(bot, markdown_content)
        return JsonResponse({"status": "ok"})


class DownloadMeetingSummaryPdfView(LoginRequiredMixin, View):
    def get(self, request, object_id, bot_object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        bot = get_object_or_404(Bot, object_id=bot_object_id, project=project)

        if bot.meeting_summary_pdf and bot.meeting_summary_pdf.name:
            return redirect(remote_storage_url(bot.meeting_summary_pdf))

        if not (bot.meeting_summary or "").strip():
            return HttpResponse("Meeting summary not found", status=404)

        if not ensure_meeting_summary_pdf(bot):
            return HttpResponse("Meeting summary PDF could not be generated", status=500)

        return redirect(remote_storage_url(bot.meeting_summary_pdf))


def _meeting_summary_docx_download_response(bot):
    summary = (bot.meeting_summary or "").strip()
    if not summary:
        return HttpResponse("Meeting summary not found", status=404)

    filename = f"{bot.object_id}_meeting_summary.docx"
    response = HttpResponse(
        build_meeting_summary_docx(bot),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


class DownloadMeetingSummaryDocxView(LoginRequiredMixin, View):
    def get(self, request, object_id, bot_object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        bot = get_object_or_404(Bot, object_id=bot_object_id, project=project)
        return _meeting_summary_docx_download_response(bot)


def _guest_expect_app_session(request):
    return "/app_sessions/" in request.path


class GuestMomPageView(View):
    """Guest-access MoM page (token in URL). No login required."""

    def get(self, request, object_id, bot_object_id, mom_guest_token):
        expect_app = _guest_expect_app_session(request)
        bot = get_bot_for_guest_mom(object_id, bot_object_id, mom_guest_token)
        assert_guest_url_matches_session_type(bot, expect_app_session=expect_app)
        meeting_summary_status_message = get_meeting_summary_availability_message(bot)
        summary_api_urls = guest_summary_api_urls(
            object_id,
            bot_object_id,
            mom_guest_token,
            expect_app_session=expect_app,
        )
        context = {
            "project": bot.project,
            "bot": bot,
            "BotStates": BotStates,
            "SessionTypes": SessionTypes,
            "summary_api_urls": summary_api_urls,
            "meeting_summary_ready": meeting_summary_is_ready(bot),
            "meeting_summary_status_message": meeting_summary_status_message,
            "meeting_summary": bot.meeting_summary or "",
            "show_meeting_summary_panel": show_meeting_summary_panel(bot),
            "can_share_guest_mom_link": False,
            "guest_mom_share_url": None,
        }
        context.update(_recordings_partial_context(bot))
        return render(request, "projects/project_guest_mom.html", context)


class GuestStreamMeetingSummaryView(View):
    def post(self, request, object_id, bot_object_id, mom_guest_token):
        expect_app = _guest_expect_app_session(request)
        bot = get_bot_for_guest_mom(object_id, bot_object_id, mom_guest_token)
        assert_guest_url_matches_session_type(bot, expect_app_session=expect_app)

        try:
            stream = generate_meeting_summary_stream(bot)
        except MeetingSummaryError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        def event_stream():
            collected = []
            try:
                for chunk in stream:
                    collected.append(chunk)
                    yield f"data: {json.dumps({'delta': chunk})}\n\n"
            except MeetingSummaryError as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            except Exception:
                logger.exception("Unexpected error during guest meeting summary streaming for bot %s", bot.object_id)
                yield f"data: {json.dumps({'error': 'An unexpected error occurred. Please try again.'})}\n\n"
            finally:
                full_text = "".join(collected).strip()
                if full_text:
                    save_meeting_summary_artifacts(bot, full_text)
                yield "data: [DONE]\n\n"

        response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


class GuestSaveMeetingSummaryView(View):
    def post(self, request, object_id, bot_object_id, mom_guest_token):
        expect_app = _guest_expect_app_session(request)
        bot = get_bot_for_guest_mom(object_id, bot_object_id, mom_guest_token)
        assert_guest_url_matches_session_type(bot, expect_app_session=expect_app)

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        markdown_content = data.get("markdown", "").strip()
        if not markdown_content:
            return JsonResponse({"error": "Summary content cannot be empty"}, status=400)

        save_meeting_summary_artifacts(bot, markdown_content)
        return JsonResponse({"status": "ok"})


class GuestDownloadMeetingSummaryPdfView(View):
    def get(self, request, object_id, bot_object_id, mom_guest_token):
        expect_app = _guest_expect_app_session(request)
        bot = get_bot_for_guest_mom(object_id, bot_object_id, mom_guest_token)
        assert_guest_url_matches_session_type(bot, expect_app_session=expect_app)

        if bot.meeting_summary_pdf and bot.meeting_summary_pdf.name:
            return redirect(remote_storage_url(bot.meeting_summary_pdf))

        if not (bot.meeting_summary or "").strip():
            return HttpResponse("Meeting summary not found", status=404)

        if not ensure_meeting_summary_pdf(bot):
            return HttpResponse("Meeting summary PDF could not be generated", status=500)

        return redirect(remote_storage_url(bot.meeting_summary_pdf))


class GuestDownloadMeetingSummaryDocxView(View):
    def get(self, request, object_id, bot_object_id, mom_guest_token):
        expect_app = _guest_expect_app_session(request)
        bot = get_bot_for_guest_mom(object_id, bot_object_id, mom_guest_token)
        assert_guest_url_matches_session_type(bot, expect_app_session=expect_app)
        return _meeting_summary_docx_download_response(bot)


class ProjectBotRecordingsView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id, bot_object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        try:
            bot = Bot.objects.select_related().get(object_id=bot_object_id, project=project)
        except Bot.DoesNotExist:
            # Redirect to bots list if bot not found
            return redirect("bots:project-bots", object_id=object_id)

        context = _recordings_partial_context(bot)

        return render(request, "projects/partials/project_bot_recordings.html", context)


class ProjectWebhooksView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        # Get or create webhook secret for the project
        webhook_secret, created = WebhookSecret.objects.get_or_create(project=project)

        context = self.get_project_context(object_id, project)
        # Only show project-level webhooks, not bot-level ones
        context["webhooks"] = project.webhook_subscriptions.filter(bot__isnull=True).order_by("-created_at")
        context["webhook_options"] = get_webhook_options_for_project(project)
        context["webhook_secret"] = base64.b64encode(webhook_secret.get_secret()).decode("utf-8")
        context["REQUIRE_HTTPS_WEBHOOKS"] = settings.REQUIRE_HTTPS_WEBHOOKS
        return render(request, "projects/project_webhooks.html", context)


class ProjectProjectView(AdminRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        context = self.get_project_context(object_id, project)
        context["users_with_access"] = project.users_with_access()
        return render(request, "projects/project_project.html", context)


class ProjectTeamView(AdminRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        # Get all users in the organization with invited_by data and their project access
        users = request.user.organization.users.select_related("invited_by").prefetch_related("project_accesses__project").order_by("-is_active", "id")

        context = self.get_project_context(object_id, project)
        context["users"] = users
        # Needed for the checkbox list for choosing which products a user can access
        context["projects"] = request.user.organization.projects.all()
        return render(request, "projects/project_team.html", context)


class EditUserView(AdminRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        user_object_id = request.POST.get("user_object_id")
        user_role = _resolve_user_role_from_post(request)
        is_active = request.POST.get("is_active") == "true"
        selected_project_ids = request.POST.getlist("project_access")

        if not user_object_id:
            return HttpResponse("User ID is required", status=400)

        # Get the user to be edited
        user_to_edit = get_object_or_404(User, object_id=user_object_id, organization=request.user.organization)

        # Prevent editing yourself
        if user_to_edit.id == request.user.id:
            return HttpResponse("You cannot edit your own account", status=400)

        # Validate project selection for non-admin roles
        if user_role != UserRole.ADMIN and not selected_project_ids:
            return HttpResponse("Please select at least one project for non-administrator users", status=400)

        # Validate that selected projects exist and belong to the organization
        if user_role != UserRole.ADMIN and selected_project_ids:
            valid_projects = Project.objects.filter(object_id__in=selected_project_ids, organization=request.user.organization)
            if len(valid_projects) != len(selected_project_ids):
                return HttpResponse("Invalid project selection", status=400)

        try:
            with transaction.atomic():
                # Update user role
                user_to_edit.role = user_role

                # Update user active status
                user_to_edit.is_active = is_active

                user_to_edit.save()

                # Update project access for non-admin roles
                if user_role != UserRole.ADMIN:
                    # Remove all existing project access
                    ProjectAccess.objects.filter(user=user_to_edit).delete()

                    # Add new project access entries
                    for project_id in selected_project_ids:
                        project_obj = Project.objects.get(object_id=project_id, organization=request.user.organization)
                        ProjectAccess.objects.create(project=project_obj, user=user_to_edit)
                else:
                    # If user is now admin, remove all project access entries
                    # since admins have access to all projects
                    ProjectAccess.objects.filter(user=user_to_edit).delete()

                # Return success response
                status_text = "active" if is_active else "disabled"
                role_labels = {
                    UserRole.ADMIN: "administrator",
                    UserRole.REGULAR_USER: "regular user",
                    UserRole.MEETING_CREATOR: "meeting creator",
                }
                role_text = role_labels[user_role]
                return HttpResponse(f"User {user_to_edit.email} has been updated successfully. Role: {role_text}, Status: {status_text}.", status=200)

        except Exception as e:
            logger.error(f"Error updating user: {str(e)}")
            return HttpResponse("An error occurred while updating the user", status=500)


class InviteUserView(AdminRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        context = self.get_project_context(object_id, project)
        return render(request, "projects/project_team.html", context)

    def post(self, request, object_id):
        get_project_for_user(user=request.user, project_object_id=object_id)
        email = request.POST.get("email")
        user_role = _resolve_user_role_from_post(request)
        selected_project_ids = request.POST.getlist("project_access")

        if not email:
            return HttpResponse("Email is required", status=400)

        # Check if user already exists
        if User.objects.filter(email=email).exists():
            return HttpResponse("A user with this email already exists", status=400)

        # Validate project selection for non-admin roles
        if user_role != UserRole.ADMIN and not selected_project_ids:
            return HttpResponse("Please select at least one project for non-administrator users", status=400)

        # Validate that selected projects exist and belong to the organization
        if user_role != UserRole.ADMIN and selected_project_ids:
            valid_projects = Project.objects.filter(object_id__in=selected_project_ids, organization=request.user.organization)
            if len(valid_projects) != len(selected_project_ids):
                return HttpResponse("Invalid project selection", status=400)

        try:
            with transaction.atomic():
                # Create the user with appropriate role
                user = User.objects.create_user(
                    email=email,
                    username=str(uuid.uuid4()),
                    organization=request.user.organization,
                    invited_by=request.user,
                    is_active=True,
                    role=user_role,
                )

                # Create project access entries for non-admin roles
                if user_role != UserRole.ADMIN and selected_project_ids:
                    for project_id in selected_project_ids:
                        project = Project.objects.get(object_id=project_id, organization=request.user.organization)
                        ProjectAccess.objects.create(project=project, user=user)

                # Send verification email
                send_email_confirmation(request, user, email=email)

                # Return success response
                return HttpResponse("Invitation sent successfully", status=200)

        except Exception as e:
            logger.error(f"Error creating invited user: {str(e)}")
            return HttpResponse("An error occurred while sending the invitation", status=500)


class CreateWebhookView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        url = request.POST.get("url")
        triggers = request.POST.getlist("triggers[]")

        # Create webhook subscription using shared function
        try:
            create_webhook_subscription(url, triggers, project, bot=None)
        except ValidationError as e:
            return HttpResponse(e.messages[0], status=400)

        # Get the project's webhook secret for response
        webhook_secret = WebhookSecret.objects.get(project=project)

        return render(
            request,
            "projects/partials/webhook_subscription_created_modal.html",
            {
                "secret": base64.b64encode(webhook_secret.get_secret()).decode("utf-8"),
                "url": url,
                "triggers": triggers,
            },
        )


class DeleteWebhookView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, View):
    def delete(self, request, object_id, webhook_object_id):
        webhook = get_webhook_subscription_for_user(user=request.user, webhook_subscription_object_id=webhook_object_id)
        webhook.delete()
        context = self.get_project_context(object_id, webhook.project)
        context["webhooks"] = WebhookSubscription.objects.filter(project=webhook.project, bot__isnull=True).order_by("-created_at")
        context["webhook_options"] = get_webhook_options_for_project(webhook.project)
        context["REQUIRE_HTTPS_WEBHOOKS"] = settings.REQUIRE_HTTPS_WEBHOOKS
        return render(request, "projects/project_webhooks.html", context)


class ResendWebhookDeliveryAttemptView(MeetingCreatorSensitiveIntegrationsDeniedMixin, View):
    def post(self, request, object_id, idempotency_key):
        # Verify user has access to this project
        get_project_for_user(user=request.user, project_object_id=object_id)

        # Get and verify access to the webhook delivery attempt
        webhook_delivery_attempt = get_webhook_delivery_attempt_for_user(
            user=request.user,
            idempotency_key=idempotency_key,
        )

        # Don't resend if the attempt count is greater than 49
        if webhook_delivery_attempt.attempt_count > 49:
            return HttpResponse(
                '<span class="badge bg-secondary">Attempts exhausted</span>',
                content_type="text/html",
            )

        # Only resend if the attempt is not pending
        if webhook_delivery_attempt.status != WebhookDeliveryAttemptStatus.PENDING:
            # Reset status to pending and queue for redelivery
            webhook_delivery_attempt.status = WebhookDeliveryAttemptStatus.PENDING
            webhook_delivery_attempt.save()

            # Queue the webhook for delivery
            deliver_webhook.delay(webhook_delivery_attempt.id)

        # Return a simple confirmation badge - user can refresh page to see final status
        return HttpResponse(
            '<span class="badge bg-warning">Pending</span>',
            content_type="text/html",
        )


class ProjectBillingView(AdminRequiredMixin, ProjectUrlContextMixin, ListView):
    template_name = "projects/project_billing.html"
    context_object_name = "transactions"
    paginate_by = 20

    def get_queryset(self):
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])
        return CreditTransaction.objects.filter(organization=project.organization).order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = get_project_for_user(user=self.request.user, project_object_id=self.kwargs["object_id"])
        context.update(self.get_project_context(self.kwargs["object_id"], project))

        # Check if organization has a valid payment method
        has_payment_method = False
        if project.organization.autopay_stripe_customer_id:
            try:
                # Retrieve the customer to check for default payment method
                customer = stripe.Customer.retrieve(
                    project.organization.autopay_stripe_customer_id,
                    api_key=os.getenv("STRIPE_SECRET_KEY"),
                )
                # Check if customer has a default payment method
                has_payment_method = customer.invoice_settings.default_payment_method is not None
            except stripe.error.StripeError:
                # If there's an error querying Stripe, assume no payment method
                has_payment_method = False

        context["has_payment_method"] = has_payment_method
        return context


class CheckoutSuccessView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def get(self, request, object_id):
        session_id = request.GET.get("session_id")
        if not session_id:
            return HttpResponse("No session ID provided", status=400)

        # Retrieve the session details
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id, api_key=os.getenv("STRIPE_SECRET_KEY"))
        except Exception as e:
            return HttpResponse(f"Error retrieving session details: {e}", status=400)

        process_checkout_session_completed(checkout_session)

        return redirect(reverse("bots:project-billing", kwargs={"object_id": object_id}))


class CreateCheckoutSessionView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        # Get the purchase amount from the form submission
        try:
            purchase_amount = float(request.POST.get("purchase_amount", 50.0))
            if purchase_amount < 1:
                purchase_amount = 1.0
        except (ValueError, TypeError):
            purchase_amount = 50.0  # Default fallback

        credit_amount = credit_amount_for_purchase_amount_dollars(purchase_amount)

        # Convert purchase amount to cents for Stripe
        unit_amount = int(purchase_amount * 100)  # in cents

        if unit_amount > 1000000:  # $10000 limit
            return HttpResponse("The maximum purchase amount is $10000.", status=400)

        # Create checkout session
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": f"{credit_amount} Boga credits",
                            "description": f"Purchase {credit_amount} Boga credits for your account",
                        },
                        "unit_amount": unit_amount,
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url=request.build_absolute_uri(reverse("bots:checkout-success", kwargs={"object_id": object_id})) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.build_absolute_uri(reverse("bots:project-billing", kwargs={"object_id": object_id})),
            metadata={
                "organization_id": str(request.user.organization.id),
                "user_id": str(request.user.id),
                "credit_amount": str(credit_amount),
            },
            api_key=os.getenv("STRIPE_SECRET_KEY"),
        )

        # Redirect directly to the Stripe checkout page
        return redirect(checkout_session.url)


def _guest_session_project():
    project_object_id = os.getenv("GUEST_SESSION_PROJECT_OBJECT_ID") or os.getenv("GUEST_PROJECT_OBJECT_ID")
    if project_object_id:
        return Project.objects.filter(object_id=project_object_id).first()

    return Project.objects.order_by("created_at").first()


def _guest_session_project_for_request(request):
    if request.user.is_authenticated:
        project = Project.accessible_to(request.user).first()
        if project:
            return project
        return Project.objects.create(name=f"{request.user.email}'s project", organization=request.user.organization)

    return _guest_session_project()


def _guest_session_zoom_settings(project, meeting_url):
    if meeting_type_from_url(meeting_url) != MeetingTypes.ZOOM:
        return None

    zoom_oauth_connection = (
        ZoomOAuthConnection.objects.filter(
            zoom_oauth_app__project=project,
            state=ZoomOAuthConnectionStates.CONNECTED,
            is_onbehalf_token_supported=True,
        )
        .order_by("-updated_at")
        .first()
    )
    if not zoom_oauth_connection:
        return None

    return {
        "sdk": "native",
        "onbehalf_token": {
            "zoom_oauth_connection_user_id": zoom_oauth_connection.user_id,
        },
    }


def _parse_guest_session_join_at(raw_join_at):
    raw_join_at = (raw_join_at or "").strip()
    if not raw_join_at:
        return None, None

    join_at = parse_datetime(raw_join_at)
    if join_at is None:
        return None, {"error": "Calendar time must be a valid date and time."}

    if timezone.is_naive(join_at):
        join_at = timezone.make_aware(join_at, timezone.get_current_timezone())

    return join_at, None


def _guest_session_clamp_join_at_to_now_if_mid_meeting(join_at, end_at):
    """If start is already past but the meeting window has not ended, join immediately (now)."""
    if join_at is None or end_at is None:
        return join_at
    now = timezone.now()
    if join_at < now < end_at:
        return now
    return join_at


def _guest_session_event_time_range(local_join_at, metadata):
    scheduled_end_at = (metadata or {}).get("scheduled_end_at")
    if not scheduled_end_at:
        return local_join_at.strftime("%H:%M")

    end_at = parse_datetime(scheduled_end_at)
    if end_at is None:
        return local_join_at.strftime("%H:%M")

    if timezone.is_naive(end_at):
        end_at = timezone.make_aware(end_at, timezone.get_current_timezone())

    local_end_at = timezone.localtime(end_at)
    return f"{local_join_at.strftime('%H:%M')} - {local_end_at.strftime('%H:%M')}"


def _guest_session_scheduled_end_at(bot):
    raw_end_at = (bot.metadata or {}).get("scheduled_end_at")
    if not raw_end_at:
        return None

    end_at = parse_datetime(raw_end_at)
    if end_at is None:
        return None

    if timezone.is_naive(end_at):
        end_at = timezone.make_aware(end_at, timezone.get_current_timezone())

    return end_at


def _guest_session_times_overlap(start_a, end_a, start_b, end_b):
    effective_end_a = end_a or start_a
    effective_end_b = end_b or start_b
    return start_a < effective_end_b and start_b < effective_end_a


def _guest_session_overlapping_scheduled_count(project, join_at, end_at):
    scheduled_bots = (
        Bot.objects.filter(project=project, join_at__isnull=False)
        .exclude(state__in=BotStates.post_meeting_states())
        .order_by("join_at")
    )
    overlapping_count = 0

    for bot in scheduled_bots:
        if _guest_session_times_overlap(join_at, end_at, bot.join_at, _guest_session_scheduled_end_at(bot)):
            overlapping_count += 1

    return overlapping_count


def _parse_guest_calendar_month(raw_month):
    today = timezone.localdate()
    if not raw_month:
        return today.year, today.month

    try:
        selected = datetime.strptime(raw_month, "%Y-%m")
    except ValueError:
        return today.year, today.month

    return selected.year, selected.month


def _guest_session_calendar(project, raw_month):
    year, month = _parse_guest_calendar_month(raw_month)
    month_start = timezone.make_aware(datetime(year, month, 1), timezone.get_current_timezone())
    next_month_year = year + 1 if month == 12 else year
    next_month = 1 if month == 12 else month + 1
    month_end = timezone.make_aware(datetime(next_month_year, next_month, 1), timezone.get_current_timezone())

    scheduled_bots = []
    if project:
        scheduled_bots = (
            Bot.objects.filter(project=project, join_at__gte=month_start, join_at__lt=month_end)
            .exclude(state__in=BotStates.post_meeting_states())
            .order_by("join_at")
        )

    events_by_day = {}
    for bot in scheduled_bots:
        local_join_at = timezone.localtime(bot.join_at)
        day_key = local_join_at.date()
        metadata = bot.metadata or {}
        events_by_day.setdefault(day_key, []).append(
            {
                "time": _guest_session_event_time_range(local_join_at, metadata),
                "name": bot.session_display_name,
                "status": BotStates(bot.state).label,
            }
        )

    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        weeks.append(
            [
                {
                    "date": day,
                    "day": day.day,
                    "in_month": day.month == month,
                    "events": events_by_day.get(day, []),
                }
                for day in week
            ]
        )

    previous_year = year - 1 if month == 1 else year
    previous_month = 12 if month == 1 else month - 1
    next_year = year + 1 if month == 12 else year
    next_month = 1 if month == 12 else month + 1

    return {
        "calendar_weeks": weeks,
        "calendar_month_label": month_start.strftime("%B %Y"),
        "previous_calendar_month": f"{previous_year:04d}-{previous_month:02d}",
        "next_calendar_month": f"{next_year:04d}-{next_month:02d}",
        "has_scheduled_sessions": any(day["events"] for week in weeks for day in week),
    }


def _guest_session_meeting_rows(project, raw_page):
    if not project:
        page = Paginator([], GUEST_SESSION_MEETINGS_PAGE_SIZE).get_page(raw_page)
        return [], page

    bots_queryset = (
        Bot.objects.filter(project=project)
        .exclude(state=BotStates.DATA_DELETED)
        .order_by("-created_at")
    )
    page = Paginator(bots_queryset, GUEST_SESSION_MEETINGS_PAGE_SIZE).get_page(raw_page)

    rows = []
    for bot in page.object_list:
        metadata = bot.metadata or {}
        meeting_at = bot.join_at or bot.created_at
        local_meeting_at = timezone.localtime(meeting_at)
        summary_urls = None
        if bot.mom_guest_token:
            summary_urls = guest_summary_api_urls(
                project.object_id,
                bot.object_id,
                bot.mom_guest_token,
                expect_app_session=False,
            )

        rows.append(
            {
                "date": local_meeting_at.strftime("%d %b %Y"),
                "time": _guest_session_event_time_range(local_meeting_at, metadata),
                "name": bot.session_display_name,
                "status": BotStates(bot.state).label,
                "session_url": guest_mom_page_absolute_url(bot),
                "docx_url": summary_urls["docx"] if summary_urls else "",
                "pdf_url": summary_urls["pdf"] if summary_urls else "",
                "downloads_available": bool((bot.meeting_summary or "").strip() or bot.meeting_summary_pdf),
            }
        )

    return rows, page


class GuestCreateSessionView(View):
    template_name = "projects/guest_create_session.html"

    def get(self, request):
        project = _guest_session_project_for_request(request)
        context = {
            "guest_limit": GUEST_SESSION_CONCURRENT_BOTS_LIMIT,
        }
        context.update(_guest_session_calendar(project, request.GET.get("month")))
        guest_session_meetings, guest_session_meetings_page = _guest_session_meeting_rows(project, request.GET.get("page"))
        context["guest_session_meetings"] = guest_session_meetings
        context["guest_session_meetings_page"] = guest_session_meetings_page
        return render(
            request,
            self.template_name,
            context,
        )

    def post(self, request):
        try:
            session_name = (request.POST.get("session_name") or "").strip()
            meeting_url = (request.POST.get("meeting_url") or "").strip()
            raw_join_at = request.POST.get("join_at")
            raw_end_at = request.POST.get("end_at")

            if not session_name:
                return JsonResponse({"error": "Session name is required."}, status=400)
            if not meeting_url:
                return JsonResponse({"error": "Meeting link is required."}, status=400)

            join_at, error = _parse_guest_session_join_at(raw_join_at)
            if error:
                return JsonResponse(error, status=400)

            end_at, error = _parse_guest_session_join_at(raw_end_at)
            if error:
                return JsonResponse({"error": "Meeting end time must be a valid date and time."}, status=400)
            if join_at and end_at and end_at <= join_at:
                return JsonResponse({"error": "Meeting end time must be after the meeting start time."}, status=400)

            join_at = _guest_session_clamp_join_at_to_now_if_mid_meeting(join_at, end_at)

            if request.user.is_authenticated:
                project = _guest_session_project_for_request(request)
                concurrent_bots_limit = None
                concurrency_error_message = None
                source = BotCreationSource.DASHBOARD
                skip_concurrency_validation = False
            else:
                project = _guest_session_project()
                if not project:
                    return JsonResponse({"error": "Guest sessions are not configured yet because no project exists."}, status=400)
                if join_at:
                    overlapping_count = _guest_session_overlapping_scheduled_count(project, join_at, end_at)
                    if overlapping_count >= GUEST_SESSION_CONCURRENT_BOTS_LIMIT:
                        return JsonResponse({"error": GUEST_SESSION_LIMIT_ERROR}, status=400)
                    concurrent_bots_limit = None
                    concurrency_error_message = None
                    skip_concurrency_validation = True
                else:
                    concurrent_bots_limit = GUEST_SESSION_CONCURRENT_BOTS_LIMIT
                    concurrency_error_message = GUEST_SESSION_LIMIT_ERROR
                    skip_concurrency_validation = False
                source = BotCreationSource.GUEST

            if not project:
                return JsonResponse({"error": "Guest sessions are not configured yet because no project exists."}, status=400)

            metadata = {
                "created_from": "guest_session_ui",
                "session_name": session_name,
            }
            if end_at:
                metadata["scheduled_end_at"] = end_at.isoformat()
            if request.user.is_authenticated:
                metadata["authenticated_user_id"] = request.user.object_id

            data = {
                "meeting_url": meeting_url,
                "bot_name": DEFAULT_BOT_NAME,
                "metadata": metadata,
            }
            zoom_settings = _guest_session_zoom_settings(project, meeting_url)
            if zoom_settings:
                data["zoom_settings"] = zoom_settings
            if join_at:
                data["join_at"] = join_at.isoformat()

            bot, error = create_bot(
                data=data,
                source=source,
                project=project,
                concurrent_bots_limit=concurrent_bots_limit,
                concurrency_error_message=concurrency_error_message,
                skip_concurrency_validation=skip_concurrency_validation,
            )
            if error:
                return JsonResponse(error, status=400)

            if bot.state == BotStates.JOINING:
                launch_bot(bot)
            elif bot.state == BotStates.SCHEDULED and bot.join_at and bot.join_at <= timezone.now():
                launch_scheduled_bot.delay(bot.id, bot.join_at.isoformat())

            return JsonResponse(
                {
                    "message": "Session created. Boga Assistant will join at the selected time." if bot.state == BotStates.SCHEDULED else "Session created. Boga Assistant is joining now.",
                    "bot_id": bot.object_id,
                    "session_url": guest_mom_page_absolute_url(bot),
                    "state": BotStates.state_to_api_code(bot.state),
                    "join_at": bot.join_at.isoformat() if bot.join_at else None,
                },
                status=201,
            )
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)


class CreateBotView(LoginRequiredMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        try:
            project = get_project_for_user(user=request.user, project_object_id=object_id)

            data = {
                "meeting_url": request.POST.get("meeting_url"),
                "bot_name": DEFAULT_BOT_NAME,
            }

            bot, error = create_bot(data=data, source=BotCreationSource.DASHBOARD, project=project)
            if error:
                return HttpResponse(json.dumps(error), status=400)

            # If this is a scheduled bot, we don't want to launch it yet.
            if bot.state == BotStates.JOINING:
                launch_bot(bot)

            return HttpResponse("ok", status=200)
        except Exception as e:
            return HttpResponse(str(e), status=400)


class CreateProjectView(AdminRequiredMixin, View):
    def post(self, request):
        name = request.POST.get("name")

        if not name:
            return HttpResponse("Project name is required", status=400)

        if len(name) > 100:
            return HttpResponse("Project name must be less than 100 characters", status=400)

        # Create a new project for the user's organization
        project = Project.objects.create(name=name, organization=request.user.organization)

        # Redirect to the new project's dashboard
        return redirect("bots:project-dashboard", object_id=project.object_id)


class EditProjectView(AdminRequiredMixin, View):
    def put(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        # Parse the request body properly for PUT requests
        put_data = QueryDict(request.body)
        name = put_data.get("name")

        if not name:
            return HttpResponse("Project name is required", status=400)

        if len(name) > 100:
            return HttpResponse("Project name must be less than 100 characters", status=400)

        # Update the project name
        project.name = name
        project.save()

        return HttpResponse("ok", status=200)


class ProjectAutopayStripePortalView(AdminRequiredMixin, View):
    def post(self, request, object_id):
        """Create or update Stripe customer and redirect to billing portal for payment method setup."""
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        organization = project.organization

        try:
            # Check if organization already has a Stripe customer
            if not organization.autopay_stripe_customer_id:
                # Create a new Stripe customer
                customer = stripe.Customer.create(
                    email=request.user.email,
                    name=organization.name,
                    metadata={
                        "organization_id": str(organization.id),
                        "user_id": str(request.user.id),
                    },
                    api_key=os.getenv("STRIPE_SECRET_KEY"),
                )

                # Save the customer ID to the organization
                organization.autopay_stripe_customer_id = customer.id
                organization.save()

            # Check if customer already has a default payment method
            customer = stripe.Customer.retrieve(
                organization.autopay_stripe_customer_id,
                api_key=os.getenv("STRIPE_SECRET_KEY"),
            )
            has_default_payment_method = customer.invoice_settings.default_payment_method is not None

            # Create billing portal session with conditional flow_data
            session_params = {
                "customer": organization.autopay_stripe_customer_id,
                "return_url": request.build_absolute_uri(reverse("projects:project-billing", args=[project.object_id])),
                "api_key": os.getenv("STRIPE_SECRET_KEY"),
            }

            # Only add flow_data if customer doesn't have a default payment method
            if not has_default_payment_method:
                session_params["flow_data"] = {"type": "payment_method_update"}

            session = stripe.billing_portal.Session.create(**session_params)

            # Redirect to the billing portal
            return redirect(session.url)

        except stripe.error.StripeError as e:
            error_id = str(uuid.uuid4())
            logger.error(f"Error setting up payment method (error_id={error_id}): {e}")
            return HttpResponse(f"Error setting up payment method. Error ID: {error_id}", status=400)
        except Exception as e:
            error_id = str(uuid.uuid4())
            logger.error(f"An error occurred setting up payment method (error_id={error_id}): {e}")
            return HttpResponse(f"An error occurred. Error ID: {error_id}", status=500)


class ProjectAutopayView(AdminRequiredMixin, View):
    def patch(self, request, object_id):
        """Update autopay settings for the organization."""
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        organization = project.organization

        try:
            # Parse JSON body
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return HttpResponse("Invalid JSON", status=400)

        # Validate and update autopay_enabled
        if "autopay_enabled" in data:
            autopay_enabled = data["autopay_enabled"]
            if not isinstance(autopay_enabled, bool):
                return HttpResponse("autopay_enabled must be a boolean", status=400)
            organization.autopay_enabled = autopay_enabled

        # Validate and update autopay_threshold_centricredits
        if "autopay_threshold_credits" in data:
            threshold_credits = data["autopay_threshold_credits"]
            if not isinstance(threshold_credits, (int, float)) or threshold_credits <= 0:
                return HttpResponse("Credit threshold must be a positive number", status=400)
            if threshold_credits > 10000:
                return HttpResponse("Credit threshold cannot exceed 10,000 credits", status=400)
            # Convert credits to centicredits
            organization.autopay_threshold_centricredits = int(threshold_credits * 100)

        # Validate and update autopay_amount_to_purchase_cents
        if "autopay_amount_dollars" in data:
            amount_dollars = data["autopay_amount_dollars"]
            if not isinstance(amount_dollars, (int, float)) or amount_dollars <= 0:
                return HttpResponse("Purchase amount must be a positive number", status=400)
            if amount_dollars < 10:
                return HttpResponse("Purchase amount must be at least $10", status=400)
            if amount_dollars > 10000:
                return HttpResponse("Purchase amount cannot exceed $10,000", status=400)
            # Convert dollars to cents
            organization.autopay_amount_to_purchase_cents = int(amount_dollars * 100)

        try:
            organization.save()
            return HttpResponse("Autopay settings updated successfully", status=200)
        except Exception as e:
            logger.error(f"Error saving autopay settings: {e}")
            return HttpResponse("Error saving autopay settings", status=500)


class CreateGoogleMeetBotLoginView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id):
        project = get_project_for_user(user=request.user, project_object_id=object_id)

        try:
            # Get or create GoogleMeetBotLoginGroup for this project
            google_meet_bot_login_group, created = GoogleMeetBotLoginGroup.objects.get_or_create(project=project)

            # Extract fields from request
            workspace_domain = request.POST.get("workspace_domain", "").strip()
            email = request.POST.get("email", "").strip()
            private_key = request.POST.get("private_key", "").strip()
            cert = request.POST.get("cert", "").strip()

            # Validate required fields
            if not all([workspace_domain, email, private_key, cert]):
                return HttpResponse("Missing required fields: workspace_domain, email, private_key, and cert are all required", status=400)

            # Create the GoogleMeetBotLogin
            google_meet_bot_login = GoogleMeetBotLogin.objects.create(
                group=google_meet_bot_login_group,
                workspace_domain=workspace_domain,
                email=email,
            )

            # Set the encrypted credentials
            credentials_data = {
                "private_key": private_key,
                "cert": cert,
            }
            google_meet_bot_login.set_credentials(credentials_data)

            context = self.get_project_context(object_id, project)
            context["google_meet_bot_login_group"] = google_meet_bot_login_group
            return render(request, "projects/partials/google_meet_bot_login_group.html", context)

        except Exception as e:
            error_id = str(uuid.uuid4())
            logger.error(f"Error creating Google Meet bot login (error_id={error_id}): {e}")
            return HttpResponse(f"Error creating Google Meet bot login. Error ID: {error_id}", status=400)


class DeleteGoogleMeetBotLoginView(MeetingCreatorSensitiveIntegrationsDeniedMixin, ProjectUrlContextMixin, View):
    def post(self, request, object_id, login_object_id):
        google_meet_bot_login = get_google_meet_bot_login_for_user(user=request.user, google_meet_bot_login_object_id=login_object_id)
        project = get_project_for_user(user=request.user, project_object_id=object_id)
        google_meet_bot_login_group = google_meet_bot_login.group
        google_meet_bot_login.delete()
        context = self.get_project_context(object_id, project)
        context["google_meet_bot_login_group"] = google_meet_bot_login_group
        return render(request, "projects/partials/google_meet_bot_login_group.html", context)
