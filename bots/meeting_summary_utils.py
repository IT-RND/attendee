import logging
import os
import re
from html import unescape as html_unescape
from io import BytesIO
from xml.sax.saxutils import escape

import requests
from django.core.files.base import ContentFile
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import BotStates, Credentials
from .utils import generate_aggregated_utterances

logger = logging.getLogger(__name__)

MAX_SUMMARY_TRANSCRIPT_CHARS = 120000


class MeetingSummaryError(Exception):
    pass


def _live_summary_states():
    return {
        BotStates.JOINED_NOT_RECORDING,
        BotStates.JOINED_RECORDING,
        BotStates.JOINED_RECORDING_PAUSED,
        BotStates.JOINED_RECORDING_PERMISSION_DENIED,
        BotStates.CONNECTED,
    }


def bot_is_in_live_meeting_state(bot):
    return bot.state in _live_summary_states()


def bot_can_generate_meeting_summary(bot):
    return bot_is_in_live_meeting_state(bot) or bot.state == BotStates.POST_PROCESSING or bot.state in BotStates.post_meeting_states()


def meeting_summary_is_ready(bot):
    if not bot_can_generate_meeting_summary(bot):
        return False

    openai_credentials = bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.OPENAI).first()
    if not openai_credentials or not openai_credentials.get_credentials():
        return False

    return bool(_build_transcript_text(bot))


def get_meeting_summary_availability_message(bot):
    if not bot_can_generate_meeting_summary(bot):
        return "Meeting summary is available after the meeting ends."

    openai_credentials = bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.OPENAI).first()
    if not openai_credentials or not openai_credentials.get_credentials():
        return "Add OpenAI credentials in project settings to generate a GPT-5.4 summary."

    if not _build_transcript_text(bot):
        if bot_is_in_live_meeting_state(bot):
            return "No transcript is available yet. Wait for transcript snippets to arrive, then generate a live summary."
        return "No transcript is available yet. Finish transcription first."

    return "Transcript is ready. Click Generate Summary to create a Bahasa Indonesia summary."


def generate_meeting_summary(bot):
    if not bot_can_generate_meeting_summary(bot):
        raise MeetingSummaryError("Meeting summary is available after the meeting ends.")

    credentials_record = bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.OPENAI).first()
    if not credentials_record:
        raise MeetingSummaryError("Add OpenAI credentials in project settings to generate a GPT-5.4 summary.")

    credentials = credentials_record.get_credentials()
    api_key = credentials.get("api_key") if credentials else None
    if not api_key:
        raise MeetingSummaryError("Add OpenAI credentials in project settings to generate a GPT-5.4 summary.")

    transcript_text = _build_transcript_text(bot)
    if not transcript_text:
        if bot_is_in_live_meeting_state(bot):
            raise MeetingSummaryError("No transcript is available yet. Wait for transcript snippets to arrive, then generate a live summary.")
        raise MeetingSummaryError("No transcript is available yet. Finish transcription first.")

    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    url = f"{base_url}/responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-5.4",
        "reasoning": {"effort": "low"},
        "input": _build_summary_prompt(bot, transcript_text),
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
    except requests.RequestException as exc:
        logger.exception("Meeting summary request failed for bot %s", bot.object_id)
        raise MeetingSummaryError("The GPT-5.4 summary request failed. Please try again.") from exc

    if response.status_code == 401:
        raise MeetingSummaryError("OpenAI credentials are invalid. Update them in project settings and try again.")

    if response.status_code != 200:
        logger.error("Meeting summary failed for bot %s with status %s: %s", bot.object_id, response.status_code, response.text)
        raise MeetingSummaryError("GPT-5.4 could not generate the summary right now. Please try again.")

    try:
        response_json = response.json()
    except ValueError as exc:
        logger.error("Meeting summary returned invalid JSON for bot %s: %s", bot.object_id, response.text)
        raise MeetingSummaryError("GPT-5.4 returned an invalid response. Please try again.") from exc

    summary_text = _extract_output_text(response_json).strip()
    if not summary_text:
        raise MeetingSummaryError("GPT-5.4 returned an empty summary. Please try again.")

    return summary_text


def generate_meeting_summary_stream(bot):
    """Generator that yields markdown chunks as they arrive from the OpenAI streaming API."""
    if not bot_can_generate_meeting_summary(bot):
        raise MeetingSummaryError("Meeting summary is available after the meeting ends.")

    credentials_record = bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.OPENAI).first()
    if not credentials_record:
        raise MeetingSummaryError("Add OpenAI credentials in project settings to generate a GPT-5.4 summary.")

    credentials = credentials_record.get_credentials()
    api_key = credentials.get("api_key") if credentials else None
    if not api_key:
        raise MeetingSummaryError("Add OpenAI credentials in project settings to generate a GPT-5.4 summary.")

    transcript_text = _build_transcript_text(bot)
    if not transcript_text:
        if bot_is_in_live_meeting_state(bot):
            raise MeetingSummaryError("No transcript is available yet. Wait for transcript snippets to arrive, then generate a live summary.")
        raise MeetingSummaryError("No transcript is available yet. Finish transcription first.")

    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    url = f"{base_url}/responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-5.4",
        "reasoning": {"effort": "low"},
        "input": _build_summary_prompt(bot, transcript_text),
        "stream": True,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=120, stream=True)
    except requests.RequestException as exc:
        logger.exception("Meeting summary streaming request failed for bot %s", bot.object_id)
        raise MeetingSummaryError("The GPT-5.4 summary request failed. Please try again.") from exc

    if response.status_code == 401:
        raise MeetingSummaryError("OpenAI credentials are invalid. Update them in project settings and try again.")

    if response.status_code != 200:
        logger.error("Meeting summary streaming failed for bot %s with status %s: %s", bot.object_id, response.status_code, response.text[:500])
        raise MeetingSummaryError("GPT-5.4 could not generate the summary right now. Please try again.")

    import json

    for line in response.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        data_str = line[len("data: "):]
        if data_str.strip() == "[DONE]":
            break
        try:
            event = json.loads(data_str)
        except (ValueError, json.JSONDecodeError):
            continue

        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta", "")
            if delta:
                yield delta


def save_meeting_summary_artifacts(bot, summary_text):
    cleaned_summary = _normalize_summary_markup((summary_text or "").strip())
    if not cleaned_summary:
        raise MeetingSummaryError("Meeting summary content cannot be empty.")

    bot.meeting_summary = cleaned_summary
    pdf_field_dirty = False

    if bot.meeting_summary_pdf and bot.meeting_summary_pdf.name:
        bot.meeting_summary_pdf.delete(save=False)
        bot.meeting_summary_pdf = None
        pdf_field_dirty = True

    try:
        pdf_content = _build_summary_pdf(bot, cleaned_summary)
    except Exception:
        logger.exception("Meeting summary PDF generation failed for bot %s", bot.object_id)
    else:
        bot.meeting_summary_pdf.save(
            f"meeting_summaries/{bot.object_id}_meeting_summary.pdf",
            ContentFile(pdf_content),
            save=False,
        )
        pdf_field_dirty = True

    update_fields = ["meeting_summary"]
    if pdf_field_dirty:
        update_fields.append("meeting_summary_pdf")
    bot.save(update_fields=update_fields)


def ensure_meeting_summary_pdf(bot):
    if not (bot.meeting_summary or "").strip():
        return False

    if bot.meeting_summary_pdf and bot.meeting_summary_pdf.name:
        return True

    save_meeting_summary_artifacts(bot, bot.meeting_summary)
    return bool(bot.meeting_summary_pdf and bot.meeting_summary_pdf.name)


def _build_transcript_text(bot):
    transcript_lines = []

    for recording in bot.recordings.all().prefetch_related("utterances__participant"):
        for utterance in generate_aggregated_utterances(recording):
            transcript = (utterance.transcription or {}).get("transcript", "").strip()
            if not transcript:
                continue

            speaker_name = utterance.participant.full_name or utterance.participant.uuid or "Unknown speaker"
            transcript_lines.append(f"{speaker_name}: {transcript}")

    transcript_text = "\n".join(transcript_lines).strip()
    if not transcript_text:
        return ""

    if len(transcript_text) > MAX_SUMMARY_TRANSCRIPT_CHARS:
        transcript_text = transcript_text[:MAX_SUMMARY_TRANSCRIPT_CHARS].rstrip() + "\n\n[Transcript truncated for summary generation.]"

    return transcript_text


def _build_summary_prompt(bot, transcript_text):
    meeting_name = bot.name or bot.object_id
    return f"""Buat ringkasan rapat berikut dalam Bahasa Indonesia menggunakan format Markdown.

Gunakan format ini:
## Ringkasan Singkat
(tulis ringkasan di sini)

## Poin Penting
- poin 1
- poin 2

## Keputusan
- keputusan 1

## Tindak Lanjut
| Tindak Lanjut | PIC | Target Waktu | Status |
| --- | --- | --- | --- |
| tindak lanjut 1 | Nama PIC | Tanggal atau belum disebutkan | Belum dimulai |

Aturan:
- Gunakan Bahasa Indonesia yang natural dan jelas.
- Gunakan format Markdown yang baik (heading, bullet points, tabel).
- Bagian "Tindak Lanjut" wajib memakai tabel Markdown dengan kolom: Tindak Lanjut, PIC, Target Waktu, Status.
- Jika detail tertentu tidak ada di transkrip, jangan mengarang.
- Jika tidak ada keputusan yang jelas, katakan belum disebutkan.
- Jika tidak ada tindak lanjut yang jelas, tetap buat tabel "Tindak Lanjut" dan isi sel dengan "Belum disebutkan" seperlunya.
- Tetap ringkas tetapi berguna untuk dibaca ulang oleh peserta rapat.

Nama rapat: {meeting_name}
Bot ID: {bot.object_id}

Transkrip:
{transcript_text}
"""


def _extract_output_text(response_json):
    output_text = response_json.get("output_text")
    if output_text:
        return output_text

    output_items = response_json.get("output") or []
    collected_text = []
    for item in output_items:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            text_value = content.get("text")
            if text_value:
                collected_text.append(text_value)

    return "\n".join(collected_text)


def _build_summary_pdf(bot, summary_text):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=f"Meeting Summary - {bot.name or bot.object_id}",
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    styles = _build_pdf_styles()
    generated_at = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M")

    story = [
        Paragraph(escape(bot.name or "Meeting Summary"), styles["SummaryTitle"]),
        Spacer(1, 4),
        Paragraph(f"Bot ID: {escape(bot.object_id)}", styles["SummaryMeta"]),
        Paragraph(f"Generated: {escape(generated_at)}", styles["SummaryMeta"]),
        Spacer(1, 10),
    ]
    story.extend(_build_pdf_story(summary_text, styles))
    doc.build(story)
    return buffer.getvalue()


def _build_pdf_styles():
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="SummaryTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            textColor=colors.HexColor("#1f2937"),
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SummaryMeta",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#6b7280"),
            spaceAfter=2,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SummaryBody",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SummaryH1",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=20,
            textColor=colors.HexColor("#111827"),
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SummaryH2",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=17,
            textColor=colors.HexColor("#111827"),
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SummaryH3",
            parent=styles["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=15,
            textColor=colors.HexColor("#111827"),
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SummaryBullet",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            leftIndent=12,
            firstLineIndent=-10,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SummaryQuote",
            parent=styles["BodyText"],
            fontName="Helvetica-Oblique",
            fontSize=10,
            leading=14,
            leftIndent=12,
            textColor=colors.HexColor("#4b5563"),
            borderPadding=4,
            borderColor=colors.HexColor("#d1d5db"),
            borderWidth=0.5,
            spaceAfter=6,
        )
    )
    return styles


def _build_pdf_story(summary_text, styles):
    story = []
    paragraph_lines = []
    table_lines = []

    def flush_paragraph():
        if not paragraph_lines:
            return
        paragraph_text = " ".join(line.strip() for line in paragraph_lines).strip()
        paragraph_lines.clear()
        if paragraph_text:
            story.append(Paragraph(_format_inline_markdown(paragraph_text), styles["SummaryBody"]))

    def flush_table():
        if not table_lines:
            return

        parsed_table = _parse_markdown_table(table_lines)
        table_lines.clear()
        if not parsed_table:
            return

        story.append(_build_pdf_table(parsed_table, styles))
        story.append(Spacer(1, 6))

    for raw_line in summary_text.splitlines():
        line = raw_line.strip()

        if not line:
            flush_paragraph()
            flush_table()
            continue

        if line.startswith("|"):
            flush_paragraph()
            table_lines.append(line)
            continue

        flush_table()

        if line.startswith("### "):
            flush_paragraph()
            story.append(Paragraph(_format_inline_markdown(line[4:]), styles["SummaryH3"]))
            continue

        if line.startswith("## "):
            flush_paragraph()
            story.append(Paragraph(_format_inline_markdown(line[3:]), styles["SummaryH2"]))
            continue

        if line.startswith("# "):
            flush_paragraph()
            story.append(Paragraph(_format_inline_markdown(line[2:]), styles["SummaryH1"]))
            continue

        checklist_match = re.match(r"^[-*]\s+\[([ xX])\]\s+(.*)$", line)
        if checklist_match:
            flush_paragraph()
            checkbox = "[x]" if checklist_match.group(1).lower() == "x" else "[ ]"
            story.append(Paragraph(f"{checkbox} {_format_inline_markdown(checklist_match.group(2))}", styles["SummaryBullet"]))
            continue

        bullet_match = re.match(r"^[-*]\s+(.*)$", line)
        if bullet_match:
            flush_paragraph()
            story.append(Paragraph(f"- {_format_inline_markdown(bullet_match.group(1))}", styles["SummaryBullet"]))
            continue

        ordered_match = re.match(r"^(\d+)\.\s+(.*)$", line)
        if ordered_match:
            flush_paragraph()
            story.append(Paragraph(f"{ordered_match.group(1)}. {_format_inline_markdown(ordered_match.group(2))}", styles["SummaryBullet"]))
            continue

        if line.startswith("> "):
            flush_paragraph()
            story.append(Paragraph(_format_inline_markdown(line[2:]), styles["SummaryQuote"]))
            continue

        paragraph_lines.append(line)

    flush_paragraph()
    flush_table()

    if not story:
        story.append(Paragraph("No summary content available.", styles["SummaryBody"]))

    return story


def _format_inline_markdown(text):
    formatted = escape(text)
    formatted = re.sub(r"`([^`]+)`", r"<font face='Courier'>\1</font>", formatted)
    formatted = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", formatted)
    formatted = re.sub(r"__([^_]+)__", r"<b>\1</b>", formatted)
    formatted = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", formatted)
    formatted = re.sub(r"(?<!_)_([^_]+)_(?!_)", r"<i>\1</i>", formatted)
    return formatted


def _normalize_summary_markup(text):
    if "<table" not in text.lower():
        return text

    return re.sub(
        r"<table\b.*?</table>",
        lambda match: _html_table_to_markdown(match.group(0)),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _parse_markdown_table(lines):
    if len(lines) < 2:
        return None

    rows = [_split_markdown_table_row(line) for line in lines]
    if any(not row for row in rows):
        return None

    separator_row = rows[1]
    if not all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in separator_row):
        return None

    header = rows[0]
    body = rows[2:] or [["Belum disebutkan" for _ in header]]
    column_count = len(header)
    normalized_rows = [header]

    for row in body:
        padded_row = row[:column_count] + [""] * max(0, column_count - len(row))
        normalized_rows.append(padded_row[:column_count])

    return normalized_rows


def _html_table_to_markdown(table_html):
    rows = _parse_html_table(table_html)
    if not rows:
        return table_html

    header = rows[0]
    separator = ["---"] * len(header)
    body = rows[1:] or [["Belum disebutkan" for _ in header]]
    markdown_rows = [header, separator, *body]
    return "\n".join(f"| {' | '.join(_escape_markdown_table_cell(cell) for cell in row)} |" for row in markdown_rows)


def _parse_html_table(table_html):
    row_matches = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.IGNORECASE | re.DOTALL)
    if not row_matches:
        return None

    rows = []
    max_columns = 0
    for row_html in row_matches:
        cell_matches = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, flags=re.IGNORECASE | re.DOTALL)
        if not cell_matches:
            continue

        cleaned_row = [_clean_html_table_cell(cell_html) for cell_html in cell_matches]
        max_columns = max(max_columns, len(cleaned_row))
        rows.append(cleaned_row)

    if not rows:
        return None

    return [row + [""] * (max_columns - len(row)) for row in rows]


def _clean_html_table_cell(cell_html):
    cleaned = re.sub(r"<br\s*/?>", "\n", cell_html, flags=re.IGNORECASE)
    cleaned = re.sub(r"</p\s*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = html_unescape(cleaned)
    cleaned = re.sub(r"\s*\n\s*", "\n", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip() or "-"


def _escape_markdown_table_cell(text):
    return (text or "-").replace("|", r"\|")


def _split_markdown_table_row(line):
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []

    if stripped.endswith("|"):
        stripped = stripped[:-1]

    stripped = stripped[1:]
    return [cell.strip() for cell in stripped.split("|")]


def _build_pdf_table(rows, styles):
    formatted_rows = []
    for row in rows:
        formatted_rows.append([Paragraph(_format_inline_markdown(cell or "-"), styles["SummaryBody"]) for cell in row])

    table = Table(formatted_rows, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
            ]
        )
    )
    return table
