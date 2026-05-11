from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.models import Bot, BotEventManager, BotEventTypes, BotStates, Project


class ManualCompleteBotSessionViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.organization = Organization.objects.create(name="Org")
        self.user = User.objects.create_user(
            username="u1",
            email="u1@example.com",
            password="pw",
            organization=self.organization,
            role=UserRole.ADMIN,
        )
        self.project = Project.objects.create(name="Proj", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="Stuck bot",
            meeting_url="https://meet.example/x",
            state=BotStates.JOINED_RECORDING,
        )
        self.client.force_login(self.user)

    def test_manual_complete_moves_to_ended(self):
        url = reverse("projects:manual-complete-bot-session", args=[self.project.object_id, self.bot.object_id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.ENDED)
        last = self.bot.last_bot_event()
        self.assertIsNotNone(last)
        self.assertEqual(last.event_type, BotEventTypes.MANUAL_SESSION_COMPLETED)

    def test_manual_complete_not_allowed_from_ready(self):
        self.bot.state = BotStates.READY
        self.bot.save(update_fields=["state"])
        url = reverse("projects:manual-complete-bot-session", args=[self.project.object_id, self.bot.object_id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.READY)


class BotEventManagerManualCompleteTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Org")
        self.project = Project.objects.create(name="Proj", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="F",
            meeting_url="https://meet.example/x",
            state=BotStates.FATAL_ERROR,
        )

    def test_fatal_error_can_manual_complete(self):
        self.assertTrue(BotEventManager.can_manual_complete_session(self.bot))
        BotEventManager.manual_complete_session_for_stuck_bot(self.bot, resolved_by_user_id=1)
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.ENDED)
