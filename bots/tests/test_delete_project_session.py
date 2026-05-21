from django.contrib.auth.models import AnonymousUser
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.bots_api_utils import delete_project_session, project_session_can_be_deleted
from bots.meeting_summary_guest_utils import user_can_delete_project_session
from bots.models import (
    AudioChunk,
    Bot,
    BotStates,
    Participant,
    Project,
    ProjectAccess,
    Recording,
    RecordingStates,
    Utterance,
)


class DeleteProjectSessionUtilsTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Org")
        self.project = Project.objects.create(name="Proj", organization=self.organization)

    def test_scheduled_session_can_be_deleted(self):
        bot = Bot.objects.create(
            project=self.project,
            name="Boga Assistant",
            meeting_url="https://meet.google.com/delete-me",
            state=BotStates.SCHEDULED,
        )
        self.assertTrue(project_session_can_be_deleted(bot))
        success, error = delete_project_session(bot)
        self.assertTrue(success)
        self.assertIsNone(error)
        self.assertFalse(Bot.objects.filter(object_id=bot.object_id).exists())

    def test_ended_session_with_transcript_cannot_be_deleted(self):
        bot = Bot.objects.create(
            project=self.project,
            name="Boga Assistant",
            meeting_url="https://meet.google.com/with-transcript",
            state=BotStates.ENDED,
        )
        participant = Participant.objects.create(bot=bot, uuid="p1", full_name="Guest")
        recording = Recording.objects.create(
            bot=bot,
            recording_type=1,
            transcription_type=1,
            state=RecordingStates.COMPLETE,
        )
        audio_chunk = AudioChunk.objects.create(
            recording=recording,
            participant=participant,
            audio_blob=b"x",
            timestamp_ms=1000,
            duration_ms=500,
            sample_rate=16000,
        )
        Utterance.objects.create(
            recording=recording,
            participant=participant,
            audio_chunk=audio_chunk,
            timestamp_ms=1000,
            duration_ms=500,
        )
        self.assertFalse(project_session_can_be_deleted(bot))

    def test_active_session_cannot_be_deleted(self):
        bot = Bot.objects.create(
            project=self.project,
            name="Boga Assistant",
            meeting_url="https://meet.google.com/live",
            state=BotStates.JOINED_RECORDING,
        )
        self.assertFalse(project_session_can_be_deleted(bot))
        success, error = delete_project_session(bot)
        self.assertFalse(success)
        self.assertIn("cannot be deleted", error["error"])


class DeleteProjectSessionDashboardViewTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Org")
        self.project = Project.objects.create(name="Proj", organization=self.organization)
        self.admin = User.objects.create_user(
            username="admin@example.com",
            email="admin@example.com",
            password="password",
            organization=self.organization,
            role=UserRole.ADMIN,
        )
        self.bot = Bot.objects.create(
            project=self.project,
            name="Boga Assistant",
            meeting_url="https://meet.google.com/dashboard-delete",
            state=BotStates.SCHEDULED,
        )
        self.client = Client()
        self.client.force_login(self.admin)

    def test_dashboard_delete_redirects_to_bots_list(self):
        url = reverse(
            "projects:delete-project-bot-session",
            kwargs={"object_id": self.project.object_id, "bot_object_id": self.bot.object_id},
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Bot.objects.filter(object_id=self.bot.object_id).exists())


class UserCanDeleteProjectSessionTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Org")
        self.project = Project.objects.create(name="Proj", organization=self.organization)
        self.admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="x",
            role=UserRole.ADMIN,
            organization=self.organization,
        )
        self.member = User.objects.create_user(
            username="member",
            email="member@example.com",
            password="x",
            role=UserRole.REGULAR_USER,
            organization=self.organization,
        )
        ProjectAccess.objects.create(project=self.project, user=self.member)
        self.outsider = User.objects.create_user(
            username="outsider",
            email="outsider@example.com",
            password="x",
            role=UserRole.REGULAR_USER,
            organization=self.organization,
        )

    def test_delete_permission_matches_share_permission(self):
        self.assertTrue(user_can_delete_project_session(self.admin, self.project))
        self.assertTrue(user_can_delete_project_session(self.member, self.project))
        self.assertFalse(user_can_delete_project_session(self.outsider, self.project))
        self.assertFalse(user_can_delete_project_session(AnonymousUser(), self.project))
