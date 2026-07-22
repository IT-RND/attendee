import os

ERP_NOTULEN_LIST_URL = os.getenv(
    "ERP_NOTULEN_LIST_URL",
    "https://webapps.boga.co.id/Transactions/NotulenMeeting.aspx",
).strip()
ERP_PARENT_ORIGIN = os.getenv("ERP_PARENT_ORIGIN", "https://alpha.boga.co.id").strip()
ERP_RETURN_ALWAYS = os.getenv("ERP_RETURN_ALWAYS", "false").lower() in ("1", "true", "yes")

ERP_RETURN_SESSION_KEY = "meetingai-erp-notulen-return"
ERP_TRX_DETAIL_SESSION_KEY = "meetingai-trx-detail-session"


def erp_return_template_context() -> dict:
    return {
        "erp_notulen_list_url": ERP_NOTULEN_LIST_URL,
        "erp_parent_origin": ERP_PARENT_ORIGIN,
        "erp_return_always": ERP_RETURN_ALWAYS,
    }
