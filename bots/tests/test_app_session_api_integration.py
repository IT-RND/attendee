import json
from unittest.mock import patch

from django.test import Client, TransactionTestCase
from rest_framework import status

from accounts.models import Organization
from bots.app_session_serializers import CreateAppSessionSerializer, normalize_zoom_rtms_dict
from bots.models import ApiKey, Bot, BotStates, Project, SessionTypes
from bots.zoom_rtms_adapter.zoom_rtms_adapter import extract_join_info


class AppSessionApiIntegrationTest(TransactionTestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="RTMS Org", centicredits=10000, is_async_transcription_enabled=True)
        self.project = Project.objects.create(name="RTMS Project", organization=self.organization)
        self.api_key, self.api_key_plain = ApiKey.create(project=self.project, name="RTMS API Key")
        self.client = Client()

    def _post(self, path, body: dict, api_key: str | None = None):
        key = api_key if api_key is not None else self.api_key_plain
        headers = {"HTTP_AUTHORIZATION": f"Token {key}", "HTTP_CONTENT_TYPE": "application/json"}
        return self.client.post(path, data=json.dumps(body), content_type="application/json", **headers)

    def test_create_app_session_accepts_server_urls_array(self):
        body = {
            "zoom_rtms": {
                "meeting_uuid": "m-uuid-1",
                "rtms_stream_id": "stream-array-1",
                "server_urls": ["wss://signaling.example/ws1", "wss://signaling.example/ws2"],
            }
        }
        response = self._post("/api/v1/app_sessions", body)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertEqual(data.get("zoom_rtms_stream_id"), "stream-array-1")
        bot = Bot.objects.get(object_id=data["id"], project=self.project)
        self.assertEqual(bot.session_type, SessionTypes.APP_SESSION)
        self.assertEqual(bot.settings["zoom_rtms"]["server_urls"], body["zoom_rtms"]["server_urls"])

    def test_create_app_session_accepts_server_urls_string(self):
        body = {
            "zoom_rtms": {
                "meeting_uuid": "m-uuid-2",
                "rtms_stream_id": "stream-str-1",
                "server_urls": "wss://signaling.example/single",
            }
        }
        response = self._post("/api/v1/app_sessions", body)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json().get("zoom_rtms_stream_id"), "stream-str-1")

    def test_create_app_session_normalizes_zoom_webhook_shape(self):
        body = {
            "zoom_rtms": {
                "event": "meeting.rtms_started",
                "payload": {
                    "meetingUuid": "m-uuid-3",
                    "rtmsStreamId": "stream-camel-1",
                    "serverUrls": ["wss://signaling.example/rtms"],
                },
            }
        }
        response = self._post("/api/v1/app_sessions", body)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        bot = Bot.objects.get(object_id=response.json()["id"])
        z = bot.settings["zoom_rtms"]
        self.assertEqual(z["meeting_uuid"], "m-uuid-3")
        self.assertEqual(z["rtms_stream_id"], "stream-camel-1")
        self.assertEqual(z["server_urls"], ["wss://signaling.example/rtms"])

    @patch("bots.app_session_api_views.launch_bot")
    def test_create_app_session_does_not_call_launch_when_state_ready(self, mock_launch):
        body = {
            "zoom_rtms": {
                "meeting_uuid": "m-uuid-4",
                "rtms_stream_id": "stream-ready",
                "server_urls": ["wss://x"],
            }
        }
        response = self._post("/api/v1/app_sessions", body)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_launch.assert_not_called()
        bot = Bot.objects.get(object_id=response.json()["id"])
        self.assertEqual(bot.state, BotStates.READY)

    def test_app_session_end_returns_400_when_zoom_rtms_missing(self):
        response = self._post("/api/v1/app_sessions/end", {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("zoom_rtms", response.json().get("error", "").lower())

    def test_app_session_end_returns_404_for_unknown_stream(self):
        response = self._post(
            "/api/v1/app_sessions/end",
            {"zoom_rtms": {"rtms_stream_id": "no-such-stream"}},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ZoomRtmsNormalizeAndSerializerTest(TransactionTestCase):
    def test_normalize_camel_case_payload(self):
        raw = {
            "payload": {
                "meetingUuid": "a",
                "rtmsStreamId": "b",
                "serverUrls": ["wss://x"],
            }
        }
        out = normalize_zoom_rtms_dict(raw)
        self.assertEqual(
            out,
            {"meeting_uuid": "a", "rtms_stream_id": "b", "server_urls": ["wss://x"]},
        )

    def test_create_app_session_serializer_validates_normalized_shape(self):
        ser = CreateAppSessionSerializer(
            data={
                "zoom_rtms": {
                    "meetingUuid": "a",
                    "rtmsStreamId": "b",
                    "serverUrls": ["wss://signaling"],
                }
            }
        )
        self.assertTrue(ser.is_valid(), ser.errors)
        self.assertEqual(
            ser.validated_data["zoom_rtms"]["meeting_uuid"],
            "a",
        )

    def test_create_app_session_serializer_rejects_empty_server_urls_array(self):
        ser = CreateAppSessionSerializer(
            data={
                "zoom_rtms": {
                    "meeting_uuid": "a",
                    "rtms_stream_id": "b",
                    "server_urls": [],
                }
            }
        )
        self.assertFalse(ser.is_valid())

    def test_extract_join_info_uses_first_url_from_server_urls_list(self):
        meeting_uuid, stream_id, signaling_url = extract_join_info(
            {
                "meeting_uuid": "m1",
                "rtms_stream_id": "s1",
                "server_urls": ["wss://a.example/rtms", "wss://b.example/rtms"],
            }
        )
        self.assertEqual(meeting_uuid, "m1")
        self.assertEqual(stream_id, "s1")
        self.assertEqual(signaling_url, "wss://a.example/rtms")
