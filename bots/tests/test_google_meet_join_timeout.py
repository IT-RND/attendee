from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from bots.automatic_leave_configuration import AutomaticLeaveConfiguration
from bots.google_meet_bot_adapter.google_meet_ui_methods import GoogleMeetUIMethods
from bots.web_bot_adapter.ui_methods import UiCouldNotJoinMeetingWaitingRoomTimeoutException
from bots.web_bot_adapter.web_bot_adapter import WebBotAdapter


class _JoinTimeoutProbe(WebBotAdapter, GoogleMeetUIMethods):
    """Minimal mixin instance for join-timeout helpers."""

    def __init__(self):
        self.automatic_leave_configuration = AutomaticLeaveConfiguration(waiting_room_timeout_seconds=900)
        self.participants_info = {"a": {}, "b": {}}
        self.driver = MagicMock()
        self.send_message_callback = MagicMock()
        self.join_attempt_started_at = None
        self.joined_at = None
        self.left_meeting = False
        self.cleaned_up = False
        self.debug_screen_recorder = None
        self.should_create_debug_recording = False


class GoogleMeetJoinTimeoutTest(SimpleTestCase):
    def test_max_join_attempt_seconds_uses_waiting_room_timeout(self):
        probe = _JoinTimeoutProbe()
        self.assertEqual(probe.max_join_attempt_seconds(), 1020)

    @patch.object(GoogleMeetUIMethods, "bot_is_in_active_call_ui", return_value=False)
    @patch.object(GoogleMeetUIMethods, "abort_join_attempt")
    def test_waiting_room_timeout_not_skipped_without_leave_call_button(self, mock_abort, _mock_in_call):
        probe = _JoinTimeoutProbe()
        started_at = 0
        with patch("bots.google_meet_bot_adapter.google_meet_ui_methods.time") as mock_time:
            mock_time.time.return_value = 2000
            with self.assertRaises(UiCouldNotJoinMeetingWaitingRoomTimeoutException):
                probe.check_if_waiting_room_timeout_exceeded(started_at, "click_captions_button")
        mock_abort.assert_called_once()

    @patch.object(GoogleMeetUIMethods, "bot_is_in_active_call_ui", return_value=True)
    @patch.object(GoogleMeetUIMethods, "abort_join_attempt")
    def test_waiting_room_timeout_skipped_when_in_call_ui(self, mock_abort, _mock_in_call):
        probe = _JoinTimeoutProbe()
        with patch("bots.google_meet_bot_adapter.google_meet_ui_methods.time") as mock_time:
            mock_time.time.return_value = 2000
            probe.check_if_waiting_room_timeout_exceeded(0, "click_captions_button")
        mock_abort.assert_not_called()
