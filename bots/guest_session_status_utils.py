"""User-facing session status labels for guest pages (Indonesian)."""

from bots.models import Bot, BotEventSubTypes, BotEventTypes, BotStates

_FAILURE_EVENT_TYPES = (
    BotEventTypes.FATAL_ERROR,
    BotEventTypes.COULD_NOT_JOIN,
    BotEventTypes.BOT_RECORDING_PERMISSION_DENIED,
)

_GUEST_EVENT_SUBTYPE_MESSAGES = {
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_NOT_STARTED_WAITING_FOR_HOST: (
        "Meeting belum dimulai atau host belum hadir."
    ),
    BotEventSubTypes.FATAL_ERROR_PROCESS_TERMINATED: (
        "Boga Assistant terhenti secara tidak terduga."
    ),
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_AUTHORIZATION_FAILED: (
        "Gagal masuk meeting Zoom (otorisasi gagal)."
    ),
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_MEETING_STATUS_FAILED: (
        "Gagal masuk meeting Zoom."
    ),
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_UNPUBLISHED_ZOOM_APP: (
        "Aplikasi Zoom belum dapat mengikuti meeting ini."
    ),
    BotEventSubTypes.FATAL_ERROR_RTMP_CONNECTION_FAILED: (
        "Koneksi streaming gagal."
    ),
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_SDK_INTERNAL_ERROR: (
        "Gagal masuk meeting Zoom (error internal)."
    ),
    BotEventSubTypes.FATAL_ERROR_UI_ELEMENT_NOT_FOUND: (
        "Boga Assistant tidak dapat mengakses halaman meeting."
    ),
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_REQUEST_TO_JOIN_DENIED: (
        "Host tidak menerima Boga Assistant (permintaan bergabung ditolak)."
    ),
    BotEventSubTypes.FATAL_ERROR_HEARTBEAT_TIMEOUT: (
        "Koneksi Boga Assistant terputus saat meeting."
    ),
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_MEETING_NOT_FOUND: (
        "Link meeting tidak ditemukan atau sudah tidak valid."
    ),
    BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED: (
        "Boga Assistant tidak dapat dijalankan."
    ),
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_WAITING_ROOM_TIMEOUT_EXCEEDED: (
        "Terlalu lama di ruang tunggu tanpa disetujui host."
    ),
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_LOGIN_REQUIRED: (
        "Meeting membutuhkan login yang tidak didukung untuk sesi tamu."
    ),
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_BOT_LOGIN_ATTEMPT_FAILED: (
        "Login ke meeting gagal."
    ),
    BotEventSubTypes.FATAL_ERROR_OUT_OF_CREDITS: (
        "Kuota sesi habis. Hubungi admin."
    ),
    BotEventSubTypes.COULD_NOT_JOIN_UNABLE_TO_CONNECT_TO_MEETING: (
        "Tidak dapat terhubung ke meeting. Periksa link dan password meeting."
    ),
    BotEventSubTypes.FATAL_ERROR_ATTENDEE_INTERNAL_ERROR: (
        "Terjadi gangguan sistem. Coba buat sesi baru atau hubungi support."
    ),
    BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_HOST_DENIED_PERMISSION: (
        "Host menolak izin rekaman untuk Boga Assistant."
    ),
    BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_REQUEST_TIMED_OUT: (
        "Host tidak merespons permintaan izin rekaman."
    ),
    BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_HOST_CLIENT_CANNOT_GRANT_PERMISSION: (
        "Perangkat host tidak dapat memberikan izin rekaman."
    ),
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_AUTHORIZED_USER_NOT_IN_MEETING_TIMEOUT_EXCEEDED: (
        "Peserta yang diizinkan belum masuk meeting dalam batas waktu."
    ),
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_BLOCKED_BY_CAPTCHA: (
        "Meeting memblokir akses otomatis (verifikasi captcha)."
    ),
}

_GUEST_STATE_LABELS = {
    BotStates.READY: "Siap",
    BotStates.SCHEDULED: "Terjadwal",
    BotStates.STAGED: "Menunggu jadwal",
    BotStates.JOINING: "Sedang bergabung",
    BotStates.WAITING_ROOM: "Menunggu ruang tunggu",
    BotStates.JOINED_NOT_RECORDING: "Sudah masuk — belum merekam",
    BotStates.JOINED_RECORDING: "Sedang merekam",
    BotStates.JOINED_RECORDING_PAUSED: "Rekaman dijeda",
    BotStates.JOINED_RECORDING_PERMISSION_DENIED: "Izin rekaman ditolak",
    BotStates.LEAVING: "Keluar dari meeting",
    BotStates.POST_PROCESSING: "Memproses hasil meeting",
    BotStates.ENDED: "Selesai",
    BotStates.DATA_DELETED: "Data dihapus",
    BotStates.JOINING_BREAKOUT_ROOM: "Masuk breakout room",
    BotStates.LEAVING_BREAKOUT_ROOM: "Keluar breakout room",
    BotStates.CONNECTING: "Menghubungkan",
    BotStates.CONNECTED: "Terhubung",
    BotStates.DISCONNECTING: "Memutus koneksi",
}

_GUEST_FAILURE_STATES = frozenset(
    {
        BotStates.FATAL_ERROR,
        BotStates.JOINED_RECORDING_PERMISSION_DENIED,
    }
)

_DEFAULT_FAILURE_MESSAGE = (
    "Boga Assistant tidak dapat mengikuti meeting. Periksa link meeting dan pastikan host menerima bot."
)

_GUEST_EVENT_SUBTYPE_SHORT_LABELS = {
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_NOT_STARTED_WAITING_FOR_HOST: "Menunggu host",
    BotEventSubTypes.FATAL_ERROR_PROCESS_TERMINATED: "Terhenti",
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_AUTHORIZATION_FAILED: "Zoom: otorisasi gagal",
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_MEETING_STATUS_FAILED: "Zoom: gagal masuk",
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_UNPUBLISHED_ZOOM_APP: "Zoom: app terbatas",
    BotEventSubTypes.FATAL_ERROR_RTMP_CONNECTION_FAILED: "Koneksi streaming gagal",
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_SDK_INTERNAL_ERROR: "Zoom: error internal",
    BotEventSubTypes.FATAL_ERROR_UI_ELEMENT_NOT_FOUND: "Gagal akses meeting",
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_REQUEST_TO_JOIN_DENIED: "Ditolak host",
    BotEventSubTypes.FATAL_ERROR_HEARTBEAT_TIMEOUT: "Koneksi terputus",
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_MEETING_NOT_FOUND: "Link tidak valid",
    BotEventSubTypes.FATAL_ERROR_BOT_NOT_LAUNCHED: "Bot tidak jalan",
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_WAITING_ROOM_TIMEOUT_EXCEEDED: "Timeout ruang tunggu",
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_LOGIN_REQUIRED: "Perlu login",
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_BOT_LOGIN_ATTEMPT_FAILED: "Login gagal",
    BotEventSubTypes.FATAL_ERROR_OUT_OF_CREDITS: "Kuota habis",
    BotEventSubTypes.COULD_NOT_JOIN_UNABLE_TO_CONNECT_TO_MEETING: "Tidak terhubung",
    BotEventSubTypes.FATAL_ERROR_ATTENDEE_INTERNAL_ERROR: "Gangguan sistem",
    BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_HOST_DENIED_PERMISSION: "Rekaman ditolak",
    BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_REQUEST_TIMED_OUT: "Izin rekaman timeout",
    BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_HOST_CLIENT_CANNOT_GRANT_PERMISSION: "Izin tidak bisa diberikan",
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_AUTHORIZED_USER_NOT_IN_MEETING_TIMEOUT_EXCEEDED: "Host belum hadir",
    BotEventSubTypes.COULD_NOT_JOIN_MEETING_BLOCKED_BY_CAPTCHA: "Diblokir captcha",
}

_DEFAULT_FAILURE_SHORT_LABEL = "Gagal masuk"


def guest_failure_event_sub_type(bot: Bot, *, annotated_sub_type=None):
    if annotated_sub_type is not None:
        return annotated_sub_type
    return (
        bot.bot_events.filter(
            event_type__in=_FAILURE_EVENT_TYPES,
            event_sub_type__isnull=False,
        )
        .order_by("-created_at")
        .values_list("event_sub_type", flat=True)
        .first()
    )


def guest_session_status_message_for_sub_type(event_sub_type) -> str | None:
    if event_sub_type is None:
        return None
    return _GUEST_EVENT_SUBTYPE_MESSAGES.get(event_sub_type)


def get_guest_session_status_label(bot: Bot, *, failure_event_sub_type=None) -> str:
    if bot.state in _GUEST_FAILURE_STATES:
        reason = guest_session_status_message_for_sub_type(
            guest_failure_event_sub_type(bot, annotated_sub_type=failure_event_sub_type)
        )
        if reason:
            return reason
        if bot.state == BotStates.FATAL_ERROR:
            return _DEFAULT_FAILURE_MESSAGE
        return _GUEST_STATE_LABELS.get(bot.state, BotStates(bot.state).label)

    return _GUEST_STATE_LABELS.get(bot.state, BotStates(bot.state).label)


def get_guest_session_status_short_label(bot: Bot, *, failure_event_sub_type=None) -> str:
    """Compact label for tables; use get_guest_session_status_label for full detail."""
    if bot.state in _GUEST_FAILURE_STATES:
        sub_type = guest_failure_event_sub_type(bot, annotated_sub_type=failure_event_sub_type)
        if sub_type is not None:
            short = _GUEST_EVENT_SUBTYPE_SHORT_LABELS.get(sub_type)
            if short:
                return short
        if bot.state == BotStates.FATAL_ERROR:
            return _DEFAULT_FAILURE_SHORT_LABEL
        return _GUEST_STATE_LABELS.get(bot.state, BotStates(bot.state).label)

    return _GUEST_STATE_LABELS.get(bot.state, BotStates(bot.state).label)
