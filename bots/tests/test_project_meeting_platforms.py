from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.bots_api_utils import BotCreationSource, create_bot
from bots.models import BotStates, Project


class ProjectMeetingPlatformTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.admin_user = User.objects.create_user(
            username="admin@example.com",
            email="admin@example.com",
            password="password",
            organization=self.organization,
            role=UserRole.ADMIN,
        )
        self.client = Client()
        self.client.force_login(self.admin_user)

    def test_edit_project_updates_meeting_platform_flags(self):
        url = reverse("projects:project-edit", kwargs={"object_id": self.project.object_id})
        response = self.client.put(
            url,
            data={
                "name": "Updated Project",
                "is_zoom_enabled": "false",
                "is_google_meet_enabled": "true",
                "is_teams_enabled": "false",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Updated Project")
        self.assertFalse(self.project.is_zoom_enabled)
        self.assertTrue(self.project.is_google_meet_enabled)
        self.assertFalse(self.project.is_teams_enabled)

    @patch("bots.bots_api_utils.launch_bot")
    def test_create_bot_rejects_disabled_zoom(self, mock_launch_bot):
        self.project.is_zoom_enabled = False
        self.project.save()

        bot, error = create_bot(
            data={
                "meeting_url": "https://zoom.us/j/123456789",
                "bot_name": "Boga Assistant",
            },
            source=BotCreationSource.API,
            project=self.project,
        )

        self.assertIsNone(bot)
        self.assertIn("Zoom meetings are disabled", error["error"])
        mock_launch_bot.assert_not_called()

    @patch("bots.bots_api_utils.launch_bot")
    def test_create_bot_allows_enabled_google_meet(self, mock_launch_bot):
        self.project.is_zoom_enabled = False
        self.project.is_teams_enabled = False
        self.project.save()

        bot, error = create_bot(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Boga Assistant",
            },
            source=BotCreationSource.API,
            project=self.project,
        )

        self.assertIsNone(error)
        self.assertIsNotNone(bot)
        mock_launch_bot.assert_called_once()
