from django.test import TestCase

from bots.guest_session_status_utils import get_guest_session_status_label
from bots.models import Bot, BotEvent, BotEventSubTypes, BotEventTypes, BotStates, Organization, Project


class GuestSessionStatusUtilsTest(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Guest Status Org")
        self.project = Project.objects.create(name="Guest Status Project", organization=organization)
        self.bot = Bot.objects.create(
            project=self.project,
            meeting_url="https://meet.google.com/abc-defg-hij",
            name="Boga Assistant",
            state=BotStates.FATAL_ERROR,
        )

    def test_scheduled_state_uses_indonesian_label(self):
        self.bot.state = BotStates.SCHEDULED
        self.bot.save(update_fields=["state"])

        self.assertEqual(get_guest_session_status_label(self.bot), "Terjadwal")

    def test_fatal_error_shows_join_denied_reason(self):
        BotEvent.objects.create(
            bot=self.bot,
            old_state=BotStates.JOINING,
            new_state=BotStates.FATAL_ERROR,
            event_type=BotEventTypes.COULD_NOT_JOIN,
            event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_REQUEST_TO_JOIN_DENIED,
        )

        self.assertEqual(
            get_guest_session_status_label(self.bot),
            "Host tidak menerima Boga Assistant (permintaan bergabung ditolak).",
        )

    def test_fatal_error_without_event_uses_default_message(self):
        self.assertEqual(
            get_guest_session_status_label(self.bot),
            "Boga Assistant tidak dapat mengikuti meeting. Periksa link meeting dan pastikan host menerima bot.",
        )
