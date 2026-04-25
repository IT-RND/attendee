import os
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import Organization, User
from bots.google_calendar_oauth import get_project_object_id_from_state
from bots.models import Calendar, CalendarPlatform, CalendarStates, Project


@override_settings(SITE_DOMAIN="localhost:8000")
class GoogleCalendarOAuthViewsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.organization = Organization.objects.create(name="Test Org")
        self.user = User.objects.create_user(
            username="calendar-admin",
            email="admin@example.com",
            password="testpass123",
            organization=self.organization,
        )
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.client.force_login(self.user)

    @patch.dict(
        os.environ,
        {
            "GOOGLE_CALENDAR_OAUTH_CLIENT_ID": "google-client-id",
            "GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET": "google-client-secret",
            "EXTERNAL_WEBHOOK_SITE_DOMAIN": "calendar.example.com",
        },
        clear=False,
    )
    def test_start_google_calendar_oauth_redirects_to_google(self):
        response = self.client.get(
            reverse("projects:project-google-calendar-oauth-start", kwargs={"object_id": self.project.object_id})
        )

        self.assertEqual(response.status_code, 302)
        parsed = urlparse(response["Location"])
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "accounts.google.com")
        self.assertEqual(parsed.path, "/o/oauth2/v2/auth")

        params = parse_qs(parsed.query)
        self.assertEqual(params["client_id"][0], "google-client-id")
        self.assertEqual(
            params["redirect_uri"][0],
            "https://calendar.example.com/projects/calendars/google/callback",
        )
        self.assertEqual(
            params["scope"][0],
            "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/userinfo.email",
        )
        self.assertEqual(
            get_project_object_id_from_state(params["state"][0]),
            self.project.object_id,
        )

    def test_start_google_calendar_oauth_redirects_back_when_not_configured(self):
        with patch.dict(
            os.environ,
            {
                "GOOGLE_CALENDAR_OAUTH_CLIENT_ID": "",
                "GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET": "",
            },
            clear=False,
        ):
            response = self.client.get(
                reverse("projects:project-google-calendar-oauth-start", kwargs={"object_id": self.project.object_id})
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("projects:project-calendars", kwargs={"object_id": self.project.object_id}), response["Location"])
        self.assertIn("google_calendar_error=", response["Location"])

    @patch("bots.google_calendar_oauth.enqueue_sync_calendar_task")
    @patch("bots.google_calendar_oauth.requests.get")
    @patch("bots.google_calendar_oauth.requests.post")
    def test_google_calendar_oauth_callback_creates_calendar(
        self,
        mock_post,
        mock_get,
        mock_enqueue_sync_calendar_task,
    ):
        with patch.dict(
            os.environ,
            {
                "GOOGLE_CALENDAR_OAUTH_CLIENT_ID": "google-client-id",
                "GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET": "google-client-secret",
                "EXTERNAL_WEBHOOK_SITE_DOMAIN": "calendar.example.com",
            },
            clear=False,
        ):
            auth_start_response = self.client.get(
                reverse("projects:project-google-calendar-oauth-start", kwargs={"object_id": self.project.object_id})
            )
            state = parse_qs(urlparse(auth_start_response["Location"]).query)["state"][0]

            token_response = Mock(status_code=200)
            token_response.json.return_value = {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
            }
            mock_post.return_value = token_response

            userinfo_response = Mock(status_code=200)
            userinfo_response.json.return_value = {"email": "calendar@example.com"}
            mock_get.return_value = userinfo_response

            response = self.client.get(
                reverse("projects:project-google-calendar-oauth-callback"),
                {"state": state, "code": "google-auth-code"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("google_calendar_success=", response["Location"])

        calendar = Calendar.objects.get(project=self.project, deduplication_key="calendar@example.com")
        self.assertEqual(calendar.platform, CalendarPlatform.GOOGLE)
        self.assertEqual(calendar.client_id, "google-client-id")
        self.assertEqual(calendar.metadata["authorized_email"], "calendar@example.com")
        self.assertEqual(
            calendar.get_credentials(),
            {
                "client_secret": "google-client-secret",
                "refresh_token": "refresh-token",
            },
        )
        mock_enqueue_sync_calendar_task.assert_called_once_with(calendar)

    @patch("bots.google_calendar_oauth.enqueue_sync_calendar_task")
    @patch("bots.google_calendar_oauth.requests.get")
    @patch("bots.google_calendar_oauth.requests.post")
    def test_google_calendar_oauth_callback_updates_existing_calendar(
        self,
        mock_post,
        mock_get,
        mock_enqueue_sync_calendar_task,
    ):
        calendar = Calendar.objects.create(
            project=self.project,
            platform=CalendarPlatform.GOOGLE,
            client_id="old-client-id",
            state=CalendarStates.DISCONNECTED,
            deduplication_key="calendar@example.com",
            metadata={"authorized_email": "calendar@example.com"},
            connection_failure_data={"error": "expired token"},
        )
        calendar.set_credentials(
            {
                "client_secret": "old-secret",
                "refresh_token": "old-refresh-token",
            }
        )

        with patch.dict(
            os.environ,
            {
                "GOOGLE_CALENDAR_OAUTH_CLIENT_ID": "google-client-id",
                "GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET": "google-client-secret",
                "EXTERNAL_WEBHOOK_SITE_DOMAIN": "calendar.example.com",
            },
            clear=False,
        ):
            auth_start_response = self.client.get(
                reverse("projects:project-google-calendar-oauth-start", kwargs={"object_id": self.project.object_id})
            )
            state = parse_qs(urlparse(auth_start_response["Location"]).query)["state"][0]

            token_response = Mock(status_code=200)
            token_response.json.return_value = {
                "access_token": "access-token",
                "refresh_token": "new-refresh-token",
            }
            mock_post.return_value = token_response

            userinfo_response = Mock(status_code=200)
            userinfo_response.json.return_value = {"email": "calendar@example.com"}
            mock_get.return_value = userinfo_response

            response = self.client.get(
                reverse("projects:project-google-calendar-oauth-callback"),
                {"state": state, "code": "google-auth-code"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Calendar.objects.filter(project=self.project).count(), 1)

        calendar.refresh_from_db()
        self.assertEqual(calendar.client_id, "google-client-id")
        self.assertEqual(calendar.state, CalendarStates.CONNECTED)
        self.assertIsNone(calendar.connection_failure_data)
        self.assertEqual(
            calendar.get_credentials(),
            {
                "client_secret": "google-client-secret",
                "refresh_token": "new-refresh-token",
            },
        )
        mock_enqueue_sync_calendar_task.assert_called_once_with(calendar)
