import json
from unittest.mock import Mock, patch

from django.test import Client, TestCase
from django.urls import reverse

from bots import meeting_summary_utils
from accounts.models import Organization, User, UserRole
from bots.models import (
    Bot,
    BotEventManager,
    BotEventTypes,
    BotStates,
    Credentials,
    Participant,
    Project,
    Recording,
    RecordingStates,
    RecordingTranscriptionStates,
    RecordingTypes,
    TranscriptionTypes,
    Utterance,
)


def mock_file_field_delete_sets_name_to_none(instance, save=True):
    instance.name = None
    if save:
        instance.instance.save()


def mock_file_field_save(instance, name, content, save=True):
    instance.name = name
    if save:
        instance.instance.save()


class MeetingSummaryFileFieldMixin:
    def start_file_field_patches(self):
        self.delete_patch = patch("django.db.models.fields.files.FieldFile.delete", autospec=True)
        self.save_patch = patch("django.db.models.fields.files.FieldFile.save", autospec=True)
        self.delete_mock = self.delete_patch.start()
        self.save_mock = self.save_patch.start()
        self.delete_mock.side_effect = mock_file_field_delete_sets_name_to_none
        self.save_mock.side_effect = mock_file_field_save
        self.addCleanup(self.delete_patch.stop)
        self.addCleanup(self.save_patch.stop)


class MeetingSummaryViewTest(MeetingSummaryFileFieldMixin, TestCase):
    def setUp(self):
        self.start_file_field_patches()
        self.client = Client()
        self.organization = Organization.objects.create(name="Test Organization")
        self.user = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="testpass123",
            organization=self.organization,
            role=UserRole.ADMIN,
        )
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="Weekly Sync",
            meeting_url="https://zoom.us/j/1234567890",
            state=BotStates.ENDED,
            settings={"recording_settings": {"format": "mp4"}},
        )
        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.AUDIO_AND_VIDEO,
            transcription_type=TranscriptionTypes.REALTIME,
            state=RecordingStates.COMPLETE,
            transcription_state=RecordingTranscriptionStates.COMPLETE,
        )
        self.participant = Participant.objects.create(
            bot=self.bot,
            uuid="speaker-1",
            full_name="Fariz",
        )
        Utterance.objects.create(
            recording=self.recording,
            participant=self.participant,
            timestamp_ms=1000,
            duration_ms=2000,
            transcription={"transcript": "Kita akan meluncurkan fitur minggu depan."},
            audio_blob=b"",
        )
        self.openai_credentials = Credentials.objects.create(
            project=self.project,
            credential_type=Credentials.CredentialTypes.OPENAI,
        )
        self.openai_credentials.set_credentials({"api_key": "test-openai-key"})
        self.client.force_login(self.user)

    @patch("bots.meeting_summary_utils.requests.post")
    def test_generate_meeting_summary_success(self, mock_post):
        mock_response = Mock(status_code=200)
        mock_response.json.return_value = {
            "output_text": "## Ringkasan Singkat\nRapat membahas peluncuran fitur minggu depan."
        }
        mock_post.return_value = mock_response

        response = self.client.post(
            reverse("projects:generate-meeting-summary", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ringkasan Singkat")
        self.assertContains(response, "Regenerate")

        self.bot.refresh_from_db()
        self.assertTrue(self.bot.meeting_summary_pdf.name.endswith(".pdf"))

        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["json"]["model"], "gpt-5.4")
        self.assertEqual(call_kwargs["json"]["reasoning"], {"effort": "low"})
        self.assertIn("Kita akan meluncurkan fitur minggu depan.", call_kwargs["json"]["input"])

    @patch("bots.meeting_summary_utils.requests.post")
    def test_generate_meeting_summary_saves_to_model(self, mock_post):
        mock_response = Mock(status_code=200)
        mock_response.json.return_value = {
            "output_text": "## Ringkasan\nTest summary content."
        }
        mock_post.return_value = mock_response

        self.client.post(
            reverse("projects:generate-meeting-summary", args=[self.project.object_id, self.bot.object_id])
        )

        self.bot.refresh_from_db()
        self.assertEqual(self.bot.meeting_summary, "## Ringkasan\nTest summary content.")
        self.assertTrue(self.bot.meeting_summary_pdf.name.endswith(".pdf"))

    def test_generate_meeting_summary_without_openai_credentials(self):
        self.openai_credentials.delete()

        response = self.client.post(
            reverse("projects:generate-meeting-summary", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add OpenAI credentials in project settings")

    def test_bot_detail_shows_generate_summary_button_when_ready(self):
        response = self.client.get(
            reverse("projects:project-bot-detail", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Meeting Summary")
        self.assertContains(response, "Generate Summary")

    def test_bot_detail_shows_saved_summary(self):
        self.bot.meeting_summary = "## Saved Summary\nThis was previously generated."
        self.bot.save(update_fields=["meeting_summary"])

        response = self.client.get(
            reverse("projects:project-bot-detail", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Saved Summary")
        self.assertContains(response, "Regenerate")

    def test_bot_detail_shows_generate_summary_button_during_post_processing(self):
        self.bot.state = BotStates.POST_PROCESSING
        self.bot.save(update_fields=["state"])

        response = self.client.get(
            reverse("projects:project-bot-detail", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Meeting Summary")
        self.assertContains(response, "Generate Summary")

    def test_meeting_end_terminates_recording_before_post_processing_completes(self):
        self.bot.state = BotStates.JOINED_RECORDING
        self.bot.save(update_fields=["state"])
        self.recording.state = RecordingStates.IN_PROGRESS
        self.recording.transcription_state = RecordingTranscriptionStates.IN_PROGRESS
        self.recording.save(update_fields=["state", "transcription_state"])

        event = BotEventManager.create_event(self.bot, BotEventTypes.MEETING_ENDED)

        self.bot.refresh_from_db()
        self.recording.refresh_from_db()

        self.assertEqual(event.new_state, BotStates.POST_PROCESSING)
        self.assertEqual(self.bot.state, BotStates.POST_PROCESSING)
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.COMPLETE)


class SaveMeetingSummaryViewTest(MeetingSummaryFileFieldMixin, TestCase):
    def setUp(self):
        self.start_file_field_patches()
        self.client = Client()
        self.organization = Organization.objects.create(name="Test Organization")
        self.user = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="testpass123",
            organization=self.organization,
            role=UserRole.ADMIN,
        )
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="Weekly Sync",
            meeting_url="https://zoom.us/j/1234567890",
            state=BotStates.ENDED,
            settings={"recording_settings": {"format": "mp4"}},
        )
        self.client.force_login(self.user)

    def test_save_summary_success(self):
        response = self.client.post(
            reverse("projects:save-meeting-summary", args=[self.project.object_id, self.bot.object_id]),
            data=json.dumps({"markdown": "## Updated Summary\n- Point 1\n- Point 2"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.meeting_summary, "## Updated Summary\n- Point 1\n- Point 2")
        self.assertTrue(self.bot.meeting_summary_pdf.name.endswith(".pdf"))

    def test_save_empty_summary_rejected(self):
        response = self.client.post(
            reverse("projects:save-meeting-summary", args=[self.project.object_id, self.bot.object_id]),
            data=json.dumps({"markdown": ""}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("error", data)

    def test_save_summary_invalid_json(self):
        response = self.client.post(
            reverse("projects:save-meeting-summary", args=[self.project.object_id, self.bot.object_id]),
            data="not json",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_save_summary_requires_authentication(self):
        self.client.logout()
        response = self.client.post(
            reverse("projects:save-meeting-summary", args=[self.project.object_id, self.bot.object_id]),
            data=json.dumps({"markdown": "## Test"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 302)

    @patch("bots.projects_views.remote_storage_url", return_value="https://example.com/summary.pdf")
    def test_download_summary_pdf_generates_missing_pdf(self, mock_remote_storage_url):
        self.bot.meeting_summary = "## Summary\nDownload me."
        self.bot.save(update_fields=["meeting_summary"])

        response = self.client.get(
            reverse("projects:download-meeting-summary-pdf", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://example.com/summary.pdf")
        self.bot.refresh_from_db()
        self.assertTrue(self.bot.meeting_summary_pdf.name.endswith(".pdf"))
        mock_remote_storage_url.assert_called_once()

    @patch("bots.projects_views.remote_storage_url", return_value="https://example.com/existing-summary.pdf")
    def test_download_summary_pdf_uses_existing_pdf_even_without_summary_text(self, mock_remote_storage_url):
        self.bot.meeting_summary_pdf.name = "meeting_summaries/existing.pdf"
        self.bot.meeting_summary = ""
        self.bot.save(update_fields=["meeting_summary", "meeting_summary_pdf"])

        response = self.client.get(
            reverse("projects:download-meeting-summary-pdf", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://example.com/existing-summary.pdf")
        mock_remote_storage_url.assert_called_once_with(self.bot.meeting_summary_pdf)

    def test_summary_prompt_requires_tindak_lanjut_table(self):
        prompt = meeting_summary_utils._build_summary_prompt(self.bot, "Fariz: Tolong follow up besok.")

        self.assertIn("| Tindak Lanjut | PIC | Target Waktu | Status |", prompt)
        self.assertIn('Bagian "Tindak Lanjut" wajib memakai tabel Markdown', prompt)

    def test_save_summary_normalizes_html_table_to_markdown(self):
        html_summary = """
## Tindak Lanjut
<table><tbody><tr><th>Tindak Lanjut</th><th>PIC</th><th>Target Waktu</th><th>Status</th></tr><tr><td>Cek PDF</td><td>Fariz</td><td>Hari ini</td><td>In Progress</td></tr></tbody></table>
""".strip()

        response = self.client.post(
            reverse("projects:save-meeting-summary", args=[self.project.object_id, self.bot.object_id]),
            data=json.dumps({"markdown": html_summary}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.bot.refresh_from_db()
        self.assertIn("| Tindak Lanjut | PIC | Target Waktu | Status |", self.bot.meeting_summary)
        self.assertNotIn("<table", self.bot.meeting_summary)


class StreamMeetingSummaryViewTest(MeetingSummaryFileFieldMixin, TestCase):
    def setUp(self):
        self.start_file_field_patches()
        self.client = Client()
        self.organization = Organization.objects.create(name="Test Organization")
        self.user = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="testpass123",
            organization=self.organization,
            role=UserRole.ADMIN,
        )
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="Weekly Sync",
            meeting_url="https://zoom.us/j/1234567890",
            state=BotStates.ENDED,
            settings={"recording_settings": {"format": "mp4"}},
        )
        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.AUDIO_AND_VIDEO,
            transcription_type=TranscriptionTypes.REALTIME,
            state=RecordingStates.COMPLETE,
            transcription_state=RecordingTranscriptionStates.COMPLETE,
        )
        self.participant = Participant.objects.create(
            bot=self.bot,
            uuid="speaker-1",
            full_name="Fariz",
        )
        Utterance.objects.create(
            recording=self.recording,
            participant=self.participant,
            timestamp_ms=1000,
            duration_ms=2000,
            transcription={"transcript": "Testing streaming."},
            audio_blob=b"",
        )
        self.openai_credentials = Credentials.objects.create(
            project=self.project,
            credential_type=Credentials.CredentialTypes.OPENAI,
        )
        self.openai_credentials.set_credentials({"api_key": "test-openai-key"})
        self.client.force_login(self.user)

    def test_stream_returns_error_without_credentials(self):
        self.openai_credentials.delete()

        response = self.client.post(
            reverse("projects:stream-meeting-summary", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("error", data)

    @patch("bots.meeting_summary_utils.requests.post")
    def test_stream_returns_sse_content_type(self, mock_post):
        mock_response = Mock(status_code=200)
        mock_response.iter_lines.return_value = [
            'data: {"type":"response.output_text.delta","delta":"Hello"}',
            'data: [DONE]',
        ]
        mock_post.return_value = mock_response

        response = self.client.post(
            reverse("projects:stream-meeting-summary", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")

    @patch("bots.meeting_summary_utils.requests.post")
    def test_stream_saves_summary_to_bot(self, mock_post):
        mock_response = Mock(status_code=200)
        mock_response.iter_lines.return_value = [
            'data: {"type":"response.output_text.delta","delta":"## Summary"}',
            'data: {"type":"response.output_text.delta","delta":"\\nHello world"}',
            'data: [DONE]',
        ]
        mock_post.return_value = mock_response

        response = self.client.post(
            reverse("projects:stream-meeting-summary", args=[self.project.object_id, self.bot.object_id])
        )

        # Consume the streaming response
        content = b"".join(response.streaming_content).decode()
        self.assertIn("[DONE]", content)

        self.bot.refresh_from_db()
        self.assertIsNotNone(self.bot.meeting_summary)
        self.assertTrue(self.bot.meeting_summary_pdf.name.endswith(".pdf"))

    def test_stream_requires_authentication(self):
        self.client.logout()
        response = self.client.post(
            reverse("projects:stream-meeting-summary", args=[self.project.object_id, self.bot.object_id])
        )
        self.assertEqual(response.status_code, 302)
