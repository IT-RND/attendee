from django.core.files.base import ContentFile
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.models import Bot, BotDebugScreenshot, BotEvent, BotEventManager, BotEventTypes, BotStates, Project


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
        self.assertTrue(response.url.endswith("#bot-join-debug"))
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.ENDED)
        last = self.bot.last_bot_event()
        self.assertIsNotNone(last)
        self.assertEqual(last.event_type, BotEventTypes.MANUAL_SESSION_COMPLETED)
        detail_url = reverse("projects:project-bot-detail", args=[self.project.object_id, self.bot.object_id])
        detail_response = self.client.get(detail_url + "#bot-join-debug")
        self.assertContains(detail_response, "bot-join-debug")

    def test_bot_detail_shows_join_debug_panel_when_stuck(self):
        self.bot.state = BotStates.JOINING
        self.bot.save(update_fields=["state"])
        detail_url = reverse("projects:project-bot-detail", args=[self.project.object_id, self.bot.object_id])
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bot-join-debug")
        self.assertContains(response, "Debug bergabung ke meeting")

    def test_bot_detail_shows_debug_video_after_failure(self):
        self.bot.state = BotStates.FATAL_ERROR
        self.bot.save(update_fields=["state"])
        event = BotEvent.objects.create(
            bot=self.bot,
            old_state=BotStates.JOINING,
            new_state=BotStates.FATAL_ERROR,
            event_type=BotEventTypes.FATAL_ERROR,
            event_metadata={"step": "click_captions_button"},
        )
        shot = BotDebugScreenshot.objects.create(bot_event=event)
        shot.file.save("debug.mp4", ContentFile(b"fake"), save=True)
        detail_url = reverse("projects:project-bot-detail", args=[self.project.object_id, self.bot.object_id])
        response = self.client.get(detail_url)
        self.assertContains(response, "click_captions_button")
        self.assertContains(response, "<video")

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
