import logging

from rest_framework import serializers

from .serializers import BotSerializer, CreateBotSerializer

logger = logging.getLogger(__name__)

import jsonschema
from drf_spectacular.utils import (
    extend_schema_field,
)


def normalize_zoom_rtms_dict(value: dict) -> dict:
    """
    Accept either the RTMS object posted by clients or a Zoom webhook envelope.

    Zoom webhooks may nest fields under `payload` and use camelCase (`meetingUuid`,
    `rtmsStreamId`, `serverUrls`).
    """
    if not isinstance(value, dict):
        raise serializers.ValidationError("zoom_rtms must be a JSON object")

    inner = value.get("payload") if isinstance(value.get("payload"), dict) else value

    meeting_uuid = inner.get("meeting_uuid") or inner.get("meetingUuid")
    rtms_stream_id = inner.get("rtms_stream_id") or inner.get("rtmsStreamId")
    server_urls = inner.get("server_urls") or inner.get("serverUrls") or inner.get("server_url")
    operator_id = inner.get("operator_id") or inner.get("operatorId")

    normalized = {
        "meeting_uuid": meeting_uuid,
        "rtms_stream_id": rtms_stream_id,
        "server_urls": server_urls,
    }
    if operator_id is not None:
        normalized["operator_id"] = operator_id

    return {k: v for k, v in normalized.items() if v is not None}


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "meeting_uuid": {
                "type": "string",
                "description": "The UUID of the Zoom meeting",
            },
            "rtms_stream_id": {
                "type": "string",
                "description": "The RTMS stream ID for the Zoom meeting",
            },
            "server_urls": {
                "oneOf": [
                    {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of server URLs for the RTMS connection (Zoom RTMS)",
                    },
                    {
                        "type": "string",
                        "description": "Single signaling/server URL (legacy or simplified clients)",
                    },
                    {
                        "type": "object",
                        "description": "Map of RTMS server URLs (e.g. signaling / all)",
                        "additionalProperties": True,
                    },
                ],
            },
        },
        "required": ["meeting_uuid", "rtms_stream_id", "server_urls"],
        "additionalProperties": False,
    }
)
class ZoomRTMSJSONField(serializers.JSONField):
    pass


class CreateAppSessionSerializer(CreateBotSerializer):
    # Remove inherited required fields that don't apply to app sessions
    meeting_url = None
    bot_name = None
    join_at = None

    zoom_rtms = ZoomRTMSJSONField(help_text="Zoom RTMS configuration containing meeting UUID, stream ID, and server URLs", required=True)

    ZOOM_RTMS_SCHEMA = {
        "type": "object",
        "properties": {
            "meeting_uuid": {"type": "string"},
            "rtms_stream_id": {"type": "string"},
            "server_urls": {
                "oneOf": [
                    {"type": "string", "minLength": 1},
                    {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    {"type": "object", "minProperties": 1},
                ]
            },
            "operator_id": {"type": "string"},
        },
        "required": ["meeting_uuid", "rtms_stream_id", "server_urls"],
        "additionalProperties": False,
    }

    class Meta(BotSerializer.Meta):
        fields = [field for field in BotSerializer.Meta.fields if field not in ["name", "meeting_url", "join_at"]] + ["zoom_rtms"]

    def validate_zoom_rtms(self, value):
        if value is None:
            raise serializers.ValidationError("zoom_rtms is required")

        normalized = normalize_zoom_rtms_dict(value)

        try:
            jsonschema.validate(instance=normalized, schema=self.ZOOM_RTMS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        return normalized

    def validate_transcription_settings(self, value):
        if value is None:
            value = {"meeting_closed_captions": {}}
        return super().validate_transcription_settings(value)

    def validate_recording_settings(self, value):
        if value is None:
            value = {}
        value["resolution"] = "720p"
        # Currently, we burn too much CPU with 1080p, so we'll only support 720p. Hopefully the RTMS Python SDK will let us support 1080p.
        return super().validate_recording_settings(value)


class AppSessionSerializer(BotSerializer):
    # Remove inherited required fields that don't apply to app sessions
    meeting_url = None
    bot_name = None
    join_at = None

    class Meta(BotSerializer.Meta):
        fields = [field for field in BotSerializer.Meta.fields if field not in ["name", "meeting_url", "join_at"]] + ["zoom_rtms_stream_id"]
