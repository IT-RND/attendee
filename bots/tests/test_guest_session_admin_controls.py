import os
from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Organization, User, UserRole
from bots.models import Bot, BotStates, Project, ProjectAccess


class GuestSessionAdminControlsTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Guest Org")
        self.guest_project = Project.objects.create(name="Guest Project", organization=self.organization)
        self.admin = User.objects.create_user(
            username="admin@example.com",
            email="admin@example.com",
            password="password",
            organization=self.organization,
            role=UserRole.ADMIN,
        )
        self.member = User.objects.create_user(
            username="member@example.com",
            email="member@example.com",
            password="password",
            organization=self.organization,
            role=UserRole.REGULAR_USER,
        )
        Project.objects.create(name="Other project", organization=self.organization)
        ProjectAccess.objects.create(project=self.guest_project, user=self.member)
        self.client = Client()
        self.guest_url = reverse("projects:guest-create-session")
        self.env_patch = patch.dict(
            os.environ,
            {"GUEST_SESSION_PROJECT_OBJECT_ID": self.guest_project.object_id},
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _scheduled_bot(self, name="Visible session", hidden=False):
        return Bot.objects.create(
            project=self.guest_project,
            meeting_url="https://meet.google.com/admin-control",
            name="Boga Assistant",
            state=BotStates.SCHEDULED,
            join_at=timezone.now() + timedelta(hours=2),
            metadata={"session_name": name},
            guest_session_hidden=hidden,
        )

    def test_anonymous_guest_does_not_see_hidden_sessions(self):
        self._scheduled_bot(name="Hidden session", hidden=True)
        self._scheduled_bot(name="Visible session", hidden=False)

        response = self.client.get(self.guest_url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Hidden session")
        self.assertContains(response, "Visible session")

    def test_admin_sees_hidden_sessions_with_admin_column(self):
        self._scheduled_bot(name="Hidden session", hidden=True)
        self.client.force_login(self.admin)

        response = self.client.get(self.guest_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hidden session")
        self.assertContains(response, "Hide from guest")
        self.assertContains(response, "Show on guest")
        self.assertContains(response, "Cancel")

    def test_admin_can_hide_session_from_guest(self):
        bot = self._scheduled_bot(name="Will hide")
        self.client.force_login(self.admin)
        url = reverse("projects:guest-toggle-session-visibility", kwargs={"bot_object_id": bot.object_id})

        response = self.client.post(url, {"hidden": "true"})

        self.assertEqual(response.status_code, 200)
        bot.refresh_from_db()
        self.assertTrue(bot.guest_session_hidden)

        self.client.logout()
        anon_response = self.client.get(self.guest_url)
        self.assertNotContains(anon_response, "Will hide")

    def test_admin_can_cancel_scheduled_session(self):
        bot = self._scheduled_bot(name="Will cancel")
        self.client.force_login(self.admin)
        url = reverse("projects:guest-cancel-scheduled-session", kwargs={"bot_object_id": bot.object_id})

        response = self.client.post(url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Bot.objects.filter(object_id=bot.object_id).exists())

    def test_project_member_with_guest_access_sees_admin_buttons(self):
        self._scheduled_bot(name="Member managed session")
        self.client.force_login(self.member)

        response = self.client.get(self.guest_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Member managed session")
        self.assertContains(response, "Hide from guest")
        self.assertContains(response, "Cancel")

    def test_ended_bot_detail_shows_hide_from_guest_button(self):
        bot = Bot.objects.create(
            project=self.guest_project,
            meeting_url="https://meet.google.com/ended-detail",
            name="Boga Assistant",
            state=BotStates.ENDED,
            metadata={"session_name": "Ended detail session"},
        )
        self.client.force_login(self.admin)
        url = reverse(
            "bots:project-bot-detail",
            kwargs={"object_id": self.guest_project.object_id, "bot_object_id": bot.object_id},
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hide from guest page")

    def test_cancel_rejects_non_scheduled_session(self):
        bot = Bot.objects.create(
            project=self.guest_project,
            meeting_url="https://meet.google.com/ended",
            name="Boga Assistant",
            state=BotStates.ENDED,
            metadata={"session_name": "Ended"},
        )
        self.client.force_login(self.admin)
        url = reverse("projects:guest-cancel-scheduled-session", kwargs={"bot_object_id": bot.object_id})

        response = self.client.post(url)

        self.assertEqual(response.status_code, 400)
        self.assertTrue(Bot.objects.filter(object_id=bot.object_id).exists())
