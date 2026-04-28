import os
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import Organization, User
from bots.models import Project, ZoomOAuthApp, ZoomOAuthConnection, ZoomOAuthConnectionStates
from bots.zoom_oauth import get_zoom_oauth_state_data


@override_settings(SITE_DOMAIN="localhost:8000")
class ZoomOAuthViewsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.organization = Organization.objects.create(name="Test Org")
        self.user = User.objects.create_user(
            username="zoom-admin",
            email="admin@example.com",
            password="testpass123",
            organization=self.organization,
        )
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="zoom-client-id")
        self.zoom_oauth_app.set_credentials({"client_secret": "zoom-client-secret", "webhook_secret": "zoom-webhook-secret"})
        self.client.force_login(self.user)

    @patch.dict(os.environ, {"EXTERNAL_WEBHOOK_SITE_DOMAIN": "zoom.example.com"}, clear=False)
    def test_start_zoom_oauth_redirects_to_zoom(self):
        response = self.client.get(reverse("projects:project-zoom-oauth-start", kwargs={"object_id": self.project.object_id}))

        self.assertEqual(response.status_code, 302)
        parsed = urlparse(response["Location"])
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "zoom.us")
        self.assertEqual(parsed.path, "/oauth/authorize")

        params = parse_qs(parsed.query)
        self.assertEqual(params["client_id"][0], "zoom-client-id")
        self.assertEqual(params["response_type"][0], "code")
        self.assertEqual(
            params["redirect_uri"][0],
            "https://zoom.example.com/projects/zoom/oauth/callback",
        )

        state_data = get_zoom_oauth_state_data(params["state"][0])
        self.assertEqual(state_data["project_object_id"], self.project.object_id)
        self.assertFalse(state_data["is_local_recording_token_supported"])
        self.assertTrue(state_data["is_onbehalf_token_supported"])

    @patch("bots.zoom_oauth.enqueue_sync_zoom_oauth_connection_task")
    @patch("bots.zoom_oauth_connections_api_utils._get_user_info")
    @patch("bots.zoom_oauth_connections_api_utils._exchange_access_code_for_tokens")
    def test_zoom_oauth_callback_creates_connection(
        self,
        mock_exchange_tokens,
        mock_get_user_info,
        mock_enqueue_sync_zoom_oauth_connection_task,
    ):
        mock_exchange_tokens.return_value = {
            "access_token": "zoom-access-token",
            "refresh_token": "zoom-refresh-token",
            "scope": "user:read:user user:read:token",
        }
        mock_get_user_info.return_value = {
            "id": "zoom-user-id",
            "account_id": "zoom-account-id",
            "status": "active",
        }

        with patch.dict(os.environ, {"EXTERNAL_WEBHOOK_SITE_DOMAIN": "zoom.example.com"}, clear=False):
            auth_start_response = self.client.get(reverse("projects:project-zoom-oauth-start", kwargs={"object_id": self.project.object_id}))
            state = parse_qs(urlparse(auth_start_response["Location"]).query)["state"][0]

            response = self.client.get(
                reverse("projects:project-zoom-oauth-callback"),
                {"state": state, "code": "zoom-auth-code"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("zoom_oauth_success=", response["Location"])

        zoom_oauth_connection = ZoomOAuthConnection.objects.get(zoom_oauth_app=self.zoom_oauth_app)
        self.assertEqual(zoom_oauth_connection.user_id, "zoom-user-id")
        self.assertEqual(zoom_oauth_connection.account_id, "zoom-account-id")
        self.assertEqual(zoom_oauth_connection.state, ZoomOAuthConnectionStates.CONNECTED)
        self.assertFalse(zoom_oauth_connection.is_local_recording_token_supported)
        self.assertTrue(zoom_oauth_connection.is_onbehalf_token_supported)
        self.assertEqual(zoom_oauth_connection.get_credentials()["refresh_token"], "zoom-refresh-token")

        mock_exchange_tokens.assert_called_once_with(
            code="zoom-auth-code",
            redirect_uri="https://zoom.example.com/projects/zoom/oauth/callback",
            client_id="zoom-client-id",
            client_secret="zoom-client-secret",
        )
        mock_get_user_info.assert_called_once_with("zoom-access-token")
        mock_enqueue_sync_zoom_oauth_connection_task.assert_called_once_with(zoom_oauth_connection)

    def test_start_zoom_oauth_redirects_back_without_app_credentials(self):
        self.zoom_oauth_app.delete()

        response = self.client.get(reverse("projects:project-zoom-oauth-start", kwargs={"object_id": self.project.object_id}))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("projects:project-credentials", kwargs={"object_id": self.project.object_id}), response["Location"])
        self.assertIn("zoom_oauth_error=", response["Location"])
