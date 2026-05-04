from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Organization, User
from bots.models import Bot, BotStates, Project, ZoomOAuthApp, ZoomOAuthConnection, ZoomOAuthConnectionStates


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
        self.assertContains(response, "Calendar visible session")
        self.assertContains(response, "Scheduled")
        self.assertContains(response, "Open")
        self.assertContains(response, f"{local_starts_at.strftime('%H:%M')} - {local_ends_at.strftime('%H:%M')}")
        self.assertNotContains(response, "API token")

    def test_get_paginates_guest_session_meetings(self):
        starts_at = timezone.now() + timedelta(days=1)
        for index in range(7):
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
        self.assertContains(response, "Paged session 6")
        self.assertContains(response, 'href="?page=2"')
        self.assertNotContains(response, "Paged session 0")

        response = self.client.get(f"{self.url}?page=2")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Paged session 0")
        self.assertContains(response, 'href="?page=1"')
        self.assertNotContains(response, "Paged session 6")

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

    @patch("bots.projects_views.launch_bot")
    def test_guest_zoom_session_uses_connected_zoom_oauth_connection_for_onbehalf_token(self, mock_launch_bot):
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
        self.assertEqual(
            bot.settings["zoom_settings"]["onbehalf_token"]["zoom_oauth_connection_user_id"],
            "zoom-user-id",
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
