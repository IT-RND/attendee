import json
import zipfile
from io import BytesIO
from unittest.mock import Mock, patch

from django.test import Client, TestCase
from django.urls import reverse

from bots import meeting_summary_utils
from accounts.models import Organization, User, UserRole
from bots.models import (
    Bot,
    BotEvent,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    Credentials,
    Participant,
    Project,
    Recording,
    RecordingStates,
    RecordingTranscriptionStates,
    RecordingTypes,
    SessionTypes,
    TranscriptionTypes,
    Utterance,
)
from bots.tasks.generate_meeting_summary_task import auto_generate_meeting_summary


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

    @patch.dict("os.environ", {"OPENAI_API_KEY": ""})
    def test_generate_meeting_summary_without_openai_credentials(self):
        self.openai_credentials.delete()

        response = self.client.post(
            reverse("projects:generate-meeting-summary", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add OpenAI credentials in project settings")

    @patch("bots.meeting_summary_utils.requests.post")
    @patch.dict("os.environ", {"OPENAI_API_KEY": "env-openai-key", "OPENAI_BASE_URL": "https://custom.openai.test/v1/"})
    def test_generate_meeting_summary_uses_env_openai_credentials(self, mock_post):
        self.openai_credentials.delete()
        mock_response = Mock(status_code=200)
        mock_response.json.return_value = {"output_text": "## Ringkasan\nGenerated with env key."}
        mock_post.return_value = mock_response

        response = self.client.post(
            reverse("projects:generate-meeting-summary", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 200)
        call_args = mock_post.call_args
        self.assertEqual(call_args[0][0], "https://custom.openai.test/v1/responses")
        self.assertEqual(call_args[1]["headers"]["Authorization"], "Bearer env-openai-key")

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

    def test_bot_detail_shows_generate_summary_button_during_joined_recording(self):
        self.bot.state = BotStates.JOINED_RECORDING
        self.bot.save(update_fields=["state"])

        response = self.client.get(
            reverse("projects:project-bot-detail", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Meeting Summary")
        self.assertContains(response, "Generate Summary")

    @patch("bots.meeting_summary_utils.requests.post")
    def test_generate_meeting_summary_during_joined_recording(self, mock_post):
        self.bot.state = BotStates.JOINED_RECORDING
        self.bot.save(update_fields=["state"])

        mock_response = Mock(status_code=200)
        mock_response.json.return_value = {
            "output_text": "## Ringkasan Singkat\nRapat sedang berjalan dan membahas peluncuran fitur."
        }
        mock_post.return_value = mock_response

        response = self.client.post(
            reverse("projects:generate-meeting-summary", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ringkasan Singkat")
        self.bot.refresh_from_db()
        self.assertIn("Rapat sedang berjalan", self.bot.meeting_summary)

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

    @patch("bots.tasks.generate_meeting_summary_task.auto_generate_meeting_summary.delay")
    def test_post_processing_completed_queues_auto_summary_generation(self, mock_delay):
        self.bot.state = BotStates.POST_PROCESSING
        self.bot.save(update_fields=["state"])

        with self.captureOnCommitCallbacks(execute=True):
            event = BotEventManager.create_event(self.bot, BotEventTypes.POST_PROCESSING_COMPLETED)

        self.bot.refresh_from_db()

        self.assertEqual(event.new_state, BotStates.ENDED)
        self.assertEqual(self.bot.state, BotStates.ENDED)
        mock_delay.assert_called_once_with(self.bot.id)

    @patch("bots.meeting_summary_utils.requests.post")
    def test_auto_generate_meeting_summary_task_saves_summary_for_ended_bot(self, mock_post):
        mock_response = Mock(status_code=200)
        mock_response.json.return_value = {
            "output_text": "## Ringkasan Singkat\nMoM otomatis setelah meeting selesai."
        }
        mock_post.return_value = mock_response

        auto_generate_meeting_summary.run(self.bot.id)

        self.bot.refresh_from_db()
        self.assertIn("MoM otomatis", self.bot.meeting_summary)
        self.assertTrue(self.bot.meeting_summary_pdf.name.endswith(".pdf"))


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

    def test_download_summary_docx_returns_generated_docx(self):
        self.bot.meeting_summary = """## Summary
- Point 1

## Tindak Lanjut
| Tindak Lanjut | PIC | Target Waktu | Status |
| --- | --- | --- | --- |
| Cek export | Fariz | Hari ini | In Progress |
"""
        self.bot.save(update_fields=["meeting_summary"])

        response = self.client.get(
            reverse("projects:download-meeting-summary-docx", args=[self.project.object_id, self.bot.object_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertIn(".docx", response["Content-Disposition"])

        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            document_xml = archive.read("word/document.xml").decode()

        self.assertIn("Summary", document_xml)
        self.assertIn("Cek export", document_xml)

    def test_summary_prompt_requires_tindak_lanjut_table(self):
        prompt = meeting_summary_utils._build_summary_prompt(self.bot, "Fariz: Tolong follow up besok.")

        self.assertIn("| Tindak Lanjut | PIC | Target Waktu | Status |", prompt)
        self.assertIn('Bagian "Tindak Lanjut" wajib memakai tabel Markdown', prompt)

    def test_summary_prompt_prefers_metadata_session_name(self):
        self.bot.metadata = {"session_name": "Rapat Koordinasi Q1"}
        self.bot.save(update_fields=["metadata"])
        prompt = meeting_summary_utils._build_summary_prompt(self.bot, "A: Halo.")

        self.assertIn("Nama rapat: Rapat Koordinasi Q1", prompt)

    def test_summary_prompt_includes_meeting_schedule_and_participants(self):
        Participant.objects.create(bot=self.bot, uuid="speaker-test", full_name="Fariz Tester")
        Participant.objects.create(bot=self.bot, uuid="speaker-2", full_name="Ada")
        self.bot.first_heartbeat_timestamp = 1_700_000_000
        self.bot.last_heartbeat_timestamp = 1_700_000_300
        self.bot.save(update_fields=["first_heartbeat_timestamp", "last_heartbeat_timestamp"])

        schedule = meeting_summary_utils._meeting_schedule_text(self.bot)
        prompt = meeting_summary_utils._build_summary_prompt(self.bot, "Ping.")

        self.assertIn("## Ringkasan Singkat", prompt)
        self.assertIn("- **Tanggal & waktu:**", prompt)
        self.assertIn("- **Peserta:**", prompt)
        self.assertNotIn("## Informasi Rapat", prompt)
        self.assertIn(schedule, prompt)
        self.assertIn("Ada, Fariz Tester", prompt)
        self.assertRegex(schedule, r"\d{1,2}\s+\w+\s+\d{4}")

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

    @patch.dict("os.environ", {"OPENAI_API_KEY": ""})
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


class GuestMomPageTest(TestCase):
    def setUp(self):
        self.client = Client()
        organization = Organization.objects.create(name="Guest MoM Org")
        self.project = Project.objects.create(name="Guest MoM Project", organization=organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="Ended Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
            state=BotStates.ENDED,
            settings={"recording_settings": {"format": "mp4"}},
            mom_guest_token="guestmom_integration_token_01",
        )

    def test_guest_mom_page_shows_friendly_fatal_error_reason(self):
        self.bot.state = BotStates.FATAL_ERROR
        self.bot.save(update_fields=["state"])
        BotEvent.objects.create(
            bot=self.bot,
            old_state=BotStates.JOINING,
            new_state=BotStates.FATAL_ERROR,
            event_type=BotEventTypes.COULD_NOT_JOIN,
            event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_REQUEST_TO_JOIN_DENIED,
        )
        url = reverse(
            "projects:guest-bot-mom-page",
            kwargs={
                "object_id": self.project.object_id,
                "bot_object_id": self.bot.object_id,
                "mom_guest_token": self.bot.mom_guest_token,
            },
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Host tidak menerima Boga Assistant (permintaan bergabung ditolak).",
        )
        self.assertNotContains(response, "Fatal Error")

    def test_guest_mom_page_200_without_login(self):
        url = reverse(
            "projects:guest-bot-mom-page",
            kwargs={
                "object_id": self.project.object_id,
                "bot_object_id": self.bot.object_id,
                "mom_guest_token": self.bot.mom_guest_token,
            },
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Guest link")
        self.assertContains(response, "Transcript")
        self.assertContains(response, "No recordings available")

    def test_guest_mom_wrong_token_returns_404(self):
        url = reverse(
            "projects:guest-bot-mom-page",
            kwargs={
                "object_id": self.project.object_id,
                "bot_object_id": self.bot.object_id,
                "mom_guest_token": "wrong-token-not-valid",
            },
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_guest_app_session_url_rejects_meeting_bot(self):
        url = reverse(
            "projects:guest-app-session-mom-page",
            kwargs={
                "object_id": self.project.object_id,
                "bot_object_id": self.bot.object_id,
                "mom_guest_token": self.bot.mom_guest_token,
            },
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_guest_bot_url_rejects_app_session(self):
        app_bot = Bot.objects.create(
            project=self.project,
            name="App Session",
            meeting_url="app_session",
            state=BotStates.ENDED,
            settings={"recording_settings": {"format": "mp4"}},
            session_type=SessionTypes.APP_SESSION,
            mom_guest_token="guestmom_app_session_token_02",
        )
        url = reverse(
            "projects:guest-bot-mom-page",
            kwargs={
                "object_id": self.project.object_id,
                "bot_object_id": app_bot.object_id,
                "mom_guest_token": app_bot.mom_guest_token,
            },
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_guest_can_download_summary_docx_without_login(self):
        self.bot.meeting_summary = "## Guest Summary\nTamu bisa mengunduh file ini."
        self.bot.save(update_fields=["meeting_summary"])

        response = self.client.get(
            reverse(
                "projects:guest-bot-mom-summary-docx",
                kwargs={
                    "object_id": self.project.object_id,
                    "bot_object_id": self.bot.object_id,
                    "mom_guest_token": self.bot.mom_guest_token,
                },
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            document_xml = archive.read("word/document.xml").decode()

        self.assertIn("Guest Summary", document_xml)
