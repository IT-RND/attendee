import uuid
from unittest import mock

from django.test import TransactionTestCase
from django.utils import timezone

from bots.models import (
    Bot,
    Organization,
    Participant,
    Project,
    Recording,
    RecordingManager,
    RecordingStates,
    RecordingTranscriptionStates,
    Utterance,
    AudioChunk,
)


class SetRecordingCompleteFromFailedTest(TransactionTestCase):
    """Tests for RecordingManager.set_recording_complete_from_failed,
    which corrects the race condition where terminate_recording marks a
    recording as FAILED before the S3 upload sets recording.file."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Proj", organization=self.organization)
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/xyz")
        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=1,
            transcription_type=1,
            state=RecordingStates.FAILED,
            transcription_state=RecordingTranscriptionStates.NOT_STARTED,
            transcription_provider=1,
        )

    def test_transitions_failed_to_complete(self):
        RecordingManager.set_recording_complete_from_failed(self.recording)

        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertIsNotNone(self.recording.completed_at)

    def test_idempotent_when_already_complete(self):
        self.recording.state = RecordingStates.COMPLETE
        self.recording.completed_at = timezone.now()
        self.recording.save()

        RecordingManager.set_recording_complete_from_failed(self.recording)

        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)

    def test_raises_for_non_failed_state(self):
        self.recording.state = RecordingStates.IN_PROGRESS
        self.recording.save()

        with self.assertRaises(ValueError):
            RecordingManager.set_recording_complete_from_failed(self.recording)

    def test_completes_in_progress_transcription_with_no_pending_utterances(self):
        self.recording.transcription_state = RecordingTranscriptionStates.IN_PROGRESS
        self.recording.save()

        RecordingManager.set_recording_complete_from_failed(self.recording)

        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.COMPLETE)

    def test_does_not_complete_transcription_with_pending_utterances(self):
        self.recording.transcription_state = RecordingTranscriptionStates.IN_PROGRESS
        self.recording.save()

        participant = Participant.objects.create(bot=self.bot, uuid=str(uuid.uuid4()))
        audio_chunk = AudioChunk.objects.create(
            recording=self.recording,
            participant=participant,
            audio_blob=b"rawpcmbytes",
            timestamp_ms=0,
            duration_ms=500,
            sample_rate=16000,
        )
        Utterance.objects.create(
            recording=self.recording,
            participant=participant,
            audio_chunk=audio_chunk,
            timestamp_ms=0,
            duration_ms=500,
            transcription=None,
        )

        RecordingManager.set_recording_complete_from_failed(self.recording)

        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.IN_PROGRESS)


class TerminateRecordingRaceConditionTest(TransactionTestCase):
    """End-to-end test simulating the race condition: terminate_recording
    marks recording as FAILED, then recording_file_saved corrects it."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Proj", organization=self.organization)
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://zoom.us/j/xyz")
        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=1,
            transcription_type=1,
            is_default_recording=True,
            state=RecordingStates.IN_PROGRESS,
            transcription_state=RecordingTranscriptionStates.NOT_STARTED,
            transcription_provider=1,
        )

    def test_terminate_then_file_saved_corrects_state(self):
        """Simulates the exact race: terminate_recording runs while
        recording.file is still empty, then recording_file_saved sets
        the file and corrects the state."""
        self.assertFalse(bool(self.recording.file))

        RecordingManager.terminate_recording(self.recording)

        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.FAILED)

        self.recording.file = "recordings/test-file.mp4"
        self.recording.save()

        if self.recording.state == RecordingStates.FAILED:
            RecordingManager.set_recording_complete_from_failed(self.recording)

        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertIsNotNone(self.recording.completed_at)
