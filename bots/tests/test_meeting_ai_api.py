from unittest.mock import MagicMock, patch

from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from accounts.models import Organization
from bots.meeting_ai_api import (
    MeetingAIError,
    from_meeting_ai_envelope,
    submit_meeting_to_meeting_ai,
)
from bots.meeting_ai_credentials_utils import resolve_meeting_ai_credentials_from_request
from bots.models import Bot, BotStates, Project
import bots.meeting_ai_api as meeting_ai_api_module


class MeetingAIEnvelopeTest(TestCase):
    def test_from_meeting_ai_envelope_success(self):
        response = MagicMock()
        response.status_code = 201
        response.json.return_value = {
            "success": True,
            "message": "Meeting AI record created successfully",
            "data": {"transaction_id": "NM/D01/202507/00001"},
            "timestamp": "2026-07-17T08:11:00+07:00",
        }

        result = from_meeting_ai_envelope(response)

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["transaction_id"], "NM/D01/202507/00001")

    def test_from_meeting_ai_envelope_error(self):
        response = MagicMock()
        response.status_code = 400
        response.json.return_value = {
            "success": False,
            "error": {"code": "VALIDATION_ERROR", "message": "title is required"},
        }

        result = from_meeting_ai_envelope(response)

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VALIDATION_ERROR")


class SubmitMeetingToMeetingAITest(TestCase):
    def setUp(self):
        meeting_ai_api_module._token_cache.clear()

    @patch("bots.meeting_ai_api.meeting_ai_create_meeting")
    def test_submit_creates_meeting(self, mock_create):
        mock_create.return_value = {
            "ok": True,
            "status": 201,
            "data": {"transaction_id": "NM/D01/202507/00001"},
        }

        transaction_id = submit_meeting_to_meeting_ai(
            title="Weekly sync",
            session_id="guestmom_token_abc",
            bot_id="bot_abc123",
            transaction_date="2026-07-17T08:11:00.000Z",
            meeting_ai_userid="alice",
            meeting_ai_password="secret",
            status="scheduled",
        )

        self.assertEqual(transaction_id, "NM/D01/202507/00001")
        mock_create.assert_called_once_with(
            {
                "title": "Weekly sync",
                "session_id": "guestmom_token_abc",
                "bot_id": "bot_abc123",
                "transaction_date": "2026-07-17T08:11:00.000Z",
                "type": "Bot",
                "status": "scheduled",
            },
            userid="alice",
            password="secret",
        )

    @patch("bots.meeting_ai_api.meeting_ai_edit_meeting")
    def test_submit_edits_when_existing_transaction_id(self, mock_edit):
        mock_edit.return_value = {
            "ok": True,
            "status": 200,
            "data": {"transaction_id": "NM/D01/202507/00001"},
        }

        transaction_id = submit_meeting_to_meeting_ai(
            title="Updated weekly sync",
            session_id="bot_abc123",
            bot_id="bot_abc123",
            transaction_date="2026-07-17T08:11:00.000Z",
            meeting_ai_userid="alice",
            meeting_ai_password="secret",
            existing_transaction_id="NM/D01/202507/00001",
        )

        self.assertEqual(transaction_id, "NM/D01/202507/00001")
        mock_edit.assert_called_once_with(
            {
                "trxid": "NM/D01/202507/00001",
                "title": "Updated weekly sync",
                "transaction_date": "2026-07-17T08:11:00.000Z",
            },
            userid="alice",
            password="secret",
        )

    @patch("bots.meeting_ai_api.meeting_ai_create_meeting")
    def test_submit_raises_when_transaction_id_missing(self, mock_create):
        mock_create.return_value = {"ok": True, "status": 201, "data": {"title": "Weekly sync"}}

        with self.assertRaises(MeetingAIError):
            submit_meeting_to_meeting_ai(
                title="Weekly sync",
                session_id="bot_abc123",
                bot_id="bot_abc123",
                transaction_date="2026-07-17T08:11:00.000Z",
                meeting_ai_userid="alice",
                meeting_ai_password="secret",
            )


class ResolveMeetingAICredentialsTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_resolve_explicit_post_credentials(self):
        request = self.factory.post(
            "/projects/guest/session",
            data={"meeting_ai_userid": "Alice.Example", "meeting_ai_password": "secret"},
        )

        userid, password = resolve_meeting_ai_credentials_from_request(request)

        self.assertEqual(userid, "alice.example")
        self.assertEqual(password, "secret")

    @patch("bots.meeting_ai_credentials_utils.resolve_deeplink_credentials")
    def test_resolve_deeplink_post_credentials(self, mock_resolve):
        mock_resolve.return_value = ("alice.example", "secret")
        request = self.factory.post(
            "/projects/guest/session",
            data={"user": "encrypted", "key": "shared-key"},
        )

        userid, password = resolve_meeting_ai_credentials_from_request(request)

        self.assertEqual(userid, "alice.example")
        self.assertEqual(password, "secret")
        mock_resolve.assert_called_once_with("encrypted", "shared-key")

    def test_resolve_returns_none_without_credentials(self):
        request = self.factory.post("/projects/guest/session", data={})

        userid, password = resolve_meeting_ai_credentials_from_request(request)

        self.assertIsNone(userid)
        self.assertIsNone(password)


class GuestCreateSessionMeetingAIIntegrationTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Guest Organization")
        self.project = Project.objects.create(name="Guest Project", organization=self.organization)
        self.client = Client()
        self.url = reverse("projects:guest-create-session")

    @patch("bots.projects_views.launch_bot")
    @patch("bots.projects_views.resolve_meeting_ai_credentials_from_request")
    @patch("bots.projects_views.submit_meeting_to_meeting_ai")
    def test_guest_submit_creates_meeting_ai_record_and_returns_transaction_id(
        self, mock_submit, mock_resolve_credentials, mock_launch_bot
    ):
        mock_resolve_credentials.return_value = ("alice", "secret")
        mock_submit.return_value = "NM/D01/202507/00001"

        response = self.client.post(
            self.url,
            data={
                "session_name": "Weekly sync",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
            },
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["transaction_id"], "NM/D01/202507/00001")

        bot = Bot.objects.get(object_id=payload["bot_id"])
        self.assertEqual(bot.metadata["transaction_id"], "NM/D01/202507/00001")
        mock_submit.assert_called_once()
        call_kwargs = mock_submit.call_args.kwargs
        self.assertEqual(call_kwargs["title"], "Weekly sync")
        self.assertEqual(call_kwargs["session_id"], bot.mom_guest_token)
        self.assertEqual(call_kwargs["bot_id"], bot.object_id)
        self.assertEqual(call_kwargs["status"], BotStates.state_to_api_code(bot.state))
        self.assertEqual(call_kwargs["meeting_ai_userid"], "alice")
        self.assertEqual(call_kwargs["meeting_ai_password"], "secret")
        self.assertIsNone(call_kwargs["existing_transaction_id"])
        mock_launch_bot.assert_called_once_with(bot)

    @patch("bots.projects_views.launch_bot")
    @patch("bots.projects_views.resolve_meeting_ai_credentials_from_request")
    @patch("bots.projects_views.submit_meeting_to_meeting_ai")
    def test_guest_submit_edits_when_transaction_id_provided(
        self, mock_submit, mock_resolve_credentials, mock_launch_bot
    ):
        mock_resolve_credentials.return_value = ("alice", "secret")
        mock_submit.return_value = "NM/D01/202507/00001"

        response = self.client.post(
            self.url,
            data={
                "session_name": "Updated weekly sync",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "transaction_id": "NM/D01/202507/00001",
            },
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["transaction_id"], "NM/D01/202507/00001")

        bot = Bot.objects.get(object_id=payload["bot_id"])
        self.assertEqual(bot.metadata["transaction_id"], "NM/D01/202507/00001")
        mock_submit.assert_called_once()
        self.assertEqual(
            mock_submit.call_args.kwargs["existing_transaction_id"],
            "NM/D01/202507/00001",
        )
        mock_launch_bot.assert_called_once_with(bot)

    @patch("bots.projects_views.resolve_meeting_ai_credentials_from_request")
    @patch("bots.projects_views.submit_meeting_to_meeting_ai")
    def test_guest_submit_returns_error_when_meeting_ai_fails(self, mock_submit, mock_resolve_credentials):
        mock_resolve_credentials.return_value = ("alice", "secret")
        mock_submit.side_effect = MeetingAIError("MeetingAI create failed", code="BAD_REQUEST", status=400)

        response = self.client.post(
            self.url,
            data={
                "session_name": "Weekly sync",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
            },
        )

        self.assertEqual(response.status_code, 502)
        self.assertIn("MeetingAI create failed", response.json()["error"])
        self.assertEqual(Bot.objects.count(), 1)

    @patch("bots.projects_views.launch_bot")
    @patch("bots.projects_views.resolve_meeting_ai_credentials_from_request", return_value=(None, None))
    def test_guest_submit_skips_meeting_ai_when_no_url_credentials(self, _mock_resolve_credentials, mock_launch_bot):
        response = self.client.post(
            self.url,
            data={
                "session_name": "Weekly sync",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
            },
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertNotIn("transaction_id", payload)
        bot = Bot.objects.get(object_id=payload["bot_id"])
        self.assertNotIn("transaction_id", bot.metadata or {})
        mock_launch_bot.assert_called_once_with(bot)
