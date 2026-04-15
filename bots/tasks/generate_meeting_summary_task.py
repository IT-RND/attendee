import logging

from celery import shared_task

from bots.meeting_summary_utils import MeetingSummaryError, generate_meeting_summary, meeting_summary_is_ready, save_meeting_summary_artifacts
from bots.models import Bot, BotStates

logger = logging.getLogger(__name__)


@shared_task(bind=True, soft_time_limit=300)
def auto_generate_meeting_summary(self, bot_id):
    try:
        bot = Bot.objects.select_related("project").get(id=bot_id)
    except Bot.DoesNotExist:
        logger.warning("Skipping auto meeting summary generation for missing bot %s", bot_id)
        return

    if bot.state != BotStates.ENDED:
        logger.info("Skipping auto meeting summary generation for bot %s in state %s", bot.object_id, BotStates.state_to_api_code(bot.state))
        return

    if not meeting_summary_is_ready(bot):
        logger.info("Skipping auto meeting summary generation for bot %s because summary prerequisites are not ready", bot.object_id)
        return

    try:
        summary_text = generate_meeting_summary(bot)
        save_meeting_summary_artifacts(bot, summary_text)
    except MeetingSummaryError as exc:
        logger.warning("Auto meeting summary generation failed for bot %s: %s", bot.object_id, exc)
    except Exception:
        logger.exception("Unexpected auto meeting summary failure for bot %s", bot.object_id)
