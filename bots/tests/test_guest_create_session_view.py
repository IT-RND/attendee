from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Organization, User
from bots.models import (
    Bot,
    BotEvent,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    Project,
    ZoomOAuthApp,
    ZoomOAuthConnection,
    ZoomOAuthConnectionStates,
)


class GuestCreateSessionViewTest(TestCase):
    def setUp(self):
        self.guest_organization = Organization.objects.create(name="Guest Organization")
        self.guest_project = Project.objects.create(name="Guest Project", organization=self.guest_organization)
        self.regular_organization = Organization.objects.create(name="Regular Organization")
        self.regular_project = Project.objects.create(name="Regular Project", organization=self.regular_organization)
        self.regular_user = User.objects.create_user(
            username="regular@example.com",
            email="regular@example.com",
            password="password",
            organization=self.regular_organization,
        )
        self.client = Client()
        self.url = reverse("projects:guest-create-session")

    def test_get_renders_guest_session_form(self):
        starts_at = timezone.now() + timedelta(days=1)
        ends_at = starts_at + timedelta(hours=1)
        Bot.objects.create(
            project=self.guest_project,
            meeting_url="https://meet.google.com/cal-enda-rxy",
            name="Boga Assistant",
            state=BotStates.SCHEDULED,
            join_at=starts_at,
            metadata={"session_name": "Calendar visible session", "scheduled_end_at": ends_at.isoformat()},
        )

        response = self.client.get(self.url)
        local_starts_at = timezone.localtime(starts_at)
        local_ends_at = timezone.localtime(ends_at)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Boga Assistant Meeting Invitation")
        self.assertContains(response, "Nama Meeting")
        self.assertContains(response, "Tanggal Meeting")
        self.assertContains(response, "Jam Meeting")
        self.assertContains(response, "Link Meeting")
        self.assertContains(response, "Status")
        self.assertContains(response, "Session")
        self.assertContains(response, "Download")
        self.assertContains(response, "Konfirmasi Meeting")
        self.assertContains(response, "Hari & Tanggal")
        self.assertContains(response, "Mendukung Zoom")
        self.assertContains(response, "Google Meet")
        self.assertContains(response, "Microsoft Teams")
        self.assertContains(response, "Calendar visible session")
        self.assertContains(response, "Terjadwal")
        self.assertContains(response, "Open")
        self.assertContains(response, f"{local_starts_at.strftime('%H:%M')} - {local_ends_at.strftime('%H:%M')}")
        self.assertNotContains(response, "API token")
        self.assertContains(response, "gmeet-bot-guide.png")
        self.assertContains(response, "guestGoogleMeetGuide")
        self.assertContains(response, "Admit entry")
        self.assertContains(response, "redirectToParentSite")
        self.assertContains(response, "NAVIGATE_TO")
        self.assertContains(response, "https://alpha.boga.co.id/WebAppsAlpha/Transactions/NotulenMeeting.aspx")

    def test_get_paginates_guest_session_meetings(self):
        starts_at = timezone.now() + timedelta(days=1)
        for index in range(21):
            Bot.objects.create(
                project=self.guest_project,
                meeting_url=f"https://meet.google.com/paged-session-{index}",
                name="Boga Assistant",
                state=BotStates.SCHEDULED,
                join_at=starts_at + timedelta(days=index),
                metadata={"session_name": f"Paged session {index}"},
            )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Paged session 20")
        self.assertContains(response, 'href="?page=2"')
        self.assertNotContains(response, "Paged session 0")

        response = self.client.get(f"{self.url}?page=2")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Paged session 0")
        self.assertContains(response, 'href="?page=1"')
        self.assertNotContains(response, "Paged session 20")

    def test_guest_create_session_enforces_three_concurrent_limit(self):
        for index in range(3):
            Bot.objects.create(
                project=self.guest_project,
                meeting_url=f"https://meet.google.com/abc-defg-hi{index}",
                name="Boga Assistant",
                state=BotStates.JOINED_RECORDING,
            )

        response = self.client.post(
            self.url,
            data={
                "session_name": "Blocked guest session",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("3 concurrent guest sessions", response.json()["error"])

    @patch("bots.projects_views.launch_scheduled_bot")
    def test_guest_past_start_clamps_to_now_when_meeting_still_ongoing(self, mock_launch_scheduled):
        fixed_now = timezone.now().replace(microsecond=0)
        join_at = (fixed_now - timedelta(minutes=30)).isoformat()
        end_at = (fixed_now + timedelta(hours=1)).isoformat()

        with patch("django.utils.timezone.now", return_value=fixed_now):
            response = self.client.post(
                self.url,
                data={
                    "session_name": "Mid meeting join",
                    "meeting_url": "https://meet.google.com/mid-meeting-join",
                    "join_at": join_at,
                    "end_at": end_at,
                },
            )

        self.assertEqual(response.status_code, 201)
        bot = Bot.objects.get(object_id=response.json()["bot_id"])
        self.assertEqual(bot.join_at, fixed_now)
        mock_launch_scheduled.delay.assert_called_once_with(bot.id, bot.join_at.isoformat())

    def test_guest_past_start_when_meeting_ended_is_rejected(self):
        fixed_now = timezone.now().replace(microsecond=0)
        join_at = (fixed_now - timedelta(hours=2)).isoformat()
        end_at = (fixed_now - timedelta(hours=1)).isoformat()

        with patch("django.utils.timezone.now", return_value=fixed_now):
            response = self.client.post(
                self.url,
                data={
                    "session_name": "Ended meeting",
                    "meeting_url": "https://meet.google.com/ended-meeting",
                    "join_at": join_at,
                    "end_at": end_at,
                },
            )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        join_at_errors = payload.get("join_at") if isinstance(payload, dict) else None
        self.assertIsNotNone(join_at_errors, msg=payload)
        self.assertIn("past", str(join_at_errors).lower())

    @patch("bots.projects_views.launch_bot")
    def test_guest_can_create_scheduled_session_without_token(self, mock_launch_bot):
        join_at_datetime = timezone.localtime(timezone.now() + timedelta(hours=1))
        end_at_datetime = join_at_datetime + timedelta(hours=1)
        join_at = join_at_datetime.strftime("%Y-%m-%dT%H:%M")
        end_at = end_at_datetime.strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            self.url,
            data={
                "session_name": "Guest scheduled session",
                "meeting_url": "https://meet.google.com/xyz-uvwx-rst",
                "join_at": join_at,
                "end_at": end_at,
            },
        )

        self.assertEqual(response.status_code, 201)
        bot = Bot.objects.get(object_id=response.json()["bot_id"])
        self.assertEqual(bot.project, self.guest_project)
        self.assertEqual(bot.state, BotStates.SCHEDULED)
        self.assertEqual(bot.metadata["session_name"], "Guest scheduled session")
        self.assertIn("scheduled_end_at", bot.metadata)
        self.assertNotIn("authenticated_user_id", bot.metadata)
        mock_launch_bot.assert_not_called()

    def test_guest_post_rejects_disabled_zoom_platform(self):
        self.guest_project.is_zoom_enabled = False
        self.guest_project.save()

        response = self.client.post(
            self.url,
            data={
                "session_name": "Disabled Zoom session",
                "meeting_url": "https://zoom.us/j/76402333351",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Zoom meetings are disabled", response.json()["error"])

    @patch("bots.projects_views.launch_bot")
    def test_guest_zoom_session_skips_onbehalf_token_for_internal_zoom_org(self, mock_launch_bot):
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.guest_project, client_id="zoom-client-id")
        zoom_oauth_app.set_credentials({"client_secret": "zoom-client-secret", "webhook_secret": ""})
        zoom_oauth_connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app,
            user_id="zoom-user-id",
            account_id="zoom-account-id",
            state=ZoomOAuthConnectionStates.CONNECTED,
            is_onbehalf_token_supported=True,
        )
        zoom_oauth_connection.set_credentials({"refresh_token": "zoom-refresh-token"})

        response = self.client.post(
            self.url,
            data={
                "session_name": "Guest Zoom session",
                "meeting_url": "https://zoom.us/j/76402333351?pwd=bS7mVcj9DFz2clTYGPa5w86uK2jaeq.1",
            },
        )

        self.assertEqual(response.status_code, 201)
        bot = Bot.objects.get(object_id=response.json()["bot_id"])
        self.assertEqual(bot.settings["zoom_settings"], {"sdk": "native"})
        mock_launch_bot.assert_called_once_with(bot)

    @patch("bots.projects_views.launch_bot")
    def test_guest_zoom_session_uses_onbehalf_token_for_multi_account_zoom_org(self, mock_launch_bot):
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.guest_project, client_id="zoom-client-id")
        zoom_oauth_app.set_credentials({"client_secret": "zoom-client-secret", "webhook_secret": ""})
        older_connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app,
            user_id="zoom-user-id-older",
            account_id="zoom-account-id-a",
            state=ZoomOAuthConnectionStates.CONNECTED,
            is_onbehalf_token_supported=True,
        )
        newer_connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app,
            user_id="zoom-user-id-newer",
            account_id="zoom-account-id-b",
            state=ZoomOAuthConnectionStates.CONNECTED,
            is_onbehalf_token_supported=True,
        )
        ZoomOAuthConnection.objects.filter(id=older_connection.id).update(updated_at=timezone.now() - timedelta(days=1))
        ZoomOAuthConnection.objects.filter(id=newer_connection.id).update(updated_at=timezone.now())

        response = self.client.post(
            self.url,
            data={
                "session_name": "Guest Zoom session external",
                "meeting_url": "https://zoom.us/j/76402333351?pwd=bS7mVcj9DFz2clTYGPa5w86uK2jaeq.1",
            },
        )

        self.assertEqual(response.status_code, 201)
        bot = Bot.objects.get(object_id=response.json()["bot_id"])
        self.assertEqual(
            bot.settings["zoom_settings"]["onbehalf_token"]["zoom_oauth_connection_user_id"],
            "zoom-user-id-newer",
        )
        mock_launch_bot.assert_called_once_with(bot)

    @patch("bots.projects_views.launch_bot")
    def test_guest_non_zoom_session_does_not_use_zoom_oauth_connection(self, mock_launch_bot):
        zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.guest_project, client_id="zoom-client-id")
        zoom_oauth_app.set_credentials({"client_secret": "zoom-client-secret", "webhook_secret": ""})
        ZoomOAuthConnection.objects.create(
            zoom_oauth_app=zoom_oauth_app,
            user_id="zoom-user-id",
            account_id="zoom-account-id",
            state=ZoomOAuthConnectionStates.CONNECTED,
            is_onbehalf_token_supported=True,
        )

        response = self.client.post(
            self.url,
            data={
                "session_name": "Guest Meet session",
                "meeting_url": "https://meet.google.com/xyz-uvwx-rst",
            },
        )

        self.assertEqual(response.status_code, 201)
        bot = Bot.objects.get(object_id=response.json()["bot_id"])
        self.assertNotIn("onbehalf_token", bot.settings["zoom_settings"])
        mock_launch_bot.assert_called_once_with(bot)

    @patch("bots.projects_views.launch_bot")
    def test_guest_scheduled_session_does_not_use_current_live_guest_limit(self, mock_launch_bot):
        for index in range(3):
            Bot.objects.create(
                project=self.guest_project,
                meeting_url=f"https://meet.google.com/live-guest-{index}",
                name="Boga Assistant",
                state=BotStates.JOINED_RECORDING,
            )

        join_at_datetime = timezone.localtime(timezone.now() + timedelta(hours=4))
        end_at_datetime = join_at_datetime + timedelta(hours=1)
        response = self.client.post(
            self.url,
            data={
                "session_name": "Future guest session",
                "meeting_url": "https://meet.google.com/future-guest-session",
                "join_at": join_at_datetime.isoformat(),
                "end_at": end_at_datetime.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 201)
        bot = Bot.objects.get(object_id=response.json()["bot_id"])
        self.assertEqual(bot.state, BotStates.SCHEDULED)
        self.assertEqual(bot.project, self.guest_project)
        mock_launch_bot.assert_not_called()

    def test_guest_scheduled_session_enforces_overlapping_scheduled_limit(self):
        join_at_datetime = timezone.localtime(timezone.now() + timedelta(hours=4))
        end_at_datetime = join_at_datetime + timedelta(hours=1)

        for index in range(3):
            Bot.objects.create(
                project=self.guest_project,
                meeting_url=f"https://meet.google.com/scheduled-overlap-{index}",
                name="Boga Assistant",
                state=BotStates.SCHEDULED,
                join_at=join_at_datetime + timedelta(minutes=index * 5),
                metadata={"scheduled_end_at": (end_at_datetime + timedelta(minutes=index * 5)).isoformat()},
            )

        response = self.client.post(
            self.url,
            data={
                "session_name": "Blocked overlapping guest session",
                "meeting_url": "https://meet.google.com/blocked-overlap-guest",
                "join_at": (join_at_datetime + timedelta(minutes=10)).isoformat(),
                "end_at": (end_at_datetime + timedelta(minutes=10)).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("3 concurrent guest sessions", response.json()["error"])

    def test_guest_session_end_time_must_be_after_start_time(self):
        starts_at = timezone.localtime(timezone.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        ends_at = timezone.localtime(timezone.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")

        response = self.client.post(
            self.url,
            data={
                "session_name": "Invalid end time",
                "meeting_url": "https://meet.google.com/xyz-uvwx-rst",
                "join_at": starts_at,
                "end_at": ends_at,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Meeting end time must be after the meeting start time.")

    @patch("bots.projects_views.launch_bot")
    def test_signed_in_user_uses_account_project_limit_without_token(self, mock_launch_bot):
        for index in range(3):
            Bot.objects.create(
                project=self.guest_project,
                meeting_url=f"https://meet.google.com/abc-defg-hi{index}",
                name="Boga Assistant",
                state=BotStates.JOINED_RECORDING,
            )

        self.client.force_login(self.regular_user)
        join_at = timezone.localtime(timezone.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            self.url,
            data={
                "session_name": "Signed in session",
                "meeting_url": "https://meet.google.com/xyz-uvwx-rst",
                "join_at": join_at,
            },
        )

        self.assertEqual(response.status_code, 201)
        bot = Bot.objects.get(object_id=response.json()["bot_id"])
        self.assertEqual(bot.project, self.regular_project)
        self.assertEqual(bot.state, BotStates.SCHEDULED)
        self.assertEqual(bot.metadata["session_name"], "Signed in session")
        self.assertEqual(bot.metadata["authenticated_user_id"], self.regular_user.object_id)
        mock_launch_bot.assert_not_called()

    def test_guest_meeting_table_shows_friendly_fatal_error_reason(self):
        bot = Bot.objects.create(
            project=self.guest_project,
            meeting_url="https://meet.google.com/fatal-guest-test",
            name="Boga Assistant",
            state=BotStates.FATAL_ERROR,
            metadata={"session_name": "Denied session"},
        )
        BotEvent.objects.create(
            bot=bot,
            old_state=BotStates.JOINING,
            new_state=BotStates.FATAL_ERROR,
            event_type=BotEventTypes.COULD_NOT_JOIN,
            event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_REQUEST_TO_JOIN_DENIED,
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Denied session")
        self.assertContains(response, "Ditolak host")
        self.assertContains(
            response,
            'title="Host tidak menerima Boga Assistant (permintaan bergabung ditolak)."',
        )
        self.assertNotContains(response, "Fatal Error")
