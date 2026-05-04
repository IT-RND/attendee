import logging
import os
import re
import zipfile
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


def _get_openai_api_key(project):
    credentials_record = project.credentials.filter(credential_type=Credentials.CredentialTypes.OPENAI).first()
    credentials = credentials_record.get_credentials() if credentials_record else None
    return (credentials or {}).get("api_key") or os.getenv("OPENAI_API_KEY")


def _get_openai_base_url():
    return os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")


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

    if not _get_openai_api_key(bot.project):
        return False

    return bool(_build_transcript_text(bot))


def get_meeting_summary_availability_message(bot):
    if not bot_can_generate_meeting_summary(bot):
        return "Meeting summary is available after the meeting ends."

    if not _get_openai_api_key(bot.project):
        return "Add OpenAI credentials in project settings or set OPENAI_API_KEY to generate a GPT-5.4 summary."

    if not _build_transcript_text(bot):
        if bot_is_in_live_meeting_state(bot):
            return "No transcript is available yet. Wait for transcript snippets to arrive, then generate a live summary."
        return "No transcript is available yet. Finish transcription first."

    return "Transcript is ready. Click Generate Summary to create a Bahasa Indonesia summary."


def generate_meeting_summary(bot):
    if not bot_can_generate_meeting_summary(bot):
        raise MeetingSummaryError("Meeting summary is available after the meeting ends.")

    api_key = _get_openai_api_key(bot.project)
    if not api_key:
        raise MeetingSummaryError("Add OpenAI credentials in project settings or set OPENAI_API_KEY to generate a GPT-5.4 summary.")

    transcript_text = _build_transcript_text(bot)
    if not transcript_text:
        if bot_is_in_live_meeting_state(bot):
            raise MeetingSummaryError("No transcript is available yet. Wait for transcript snippets to arrive, then generate a live summary.")
        raise MeetingSummaryError("No transcript is available yet. Finish transcription first.")

    base_url = _get_openai_base_url()
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

    api_key = _get_openai_api_key(bot.project)
    if not api_key:
        raise MeetingSummaryError("Add OpenAI credentials in project settings or set OPENAI_API_KEY to generate a GPT-5.4 summary.")

    transcript_text = _build_transcript_text(bot)
    if not transcript_text:
        if bot_is_in_live_meeting_state(bot):
            raise MeetingSummaryError("No transcript is available yet. Wait for transcript snippets to arrive, then generate a live summary.")
        raise MeetingSummaryError("No transcript is available yet. Finish transcription first.")

    base_url = _get_openai_base_url()
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


def build_meeting_summary_docx(bot):
    cleaned_summary = _normalize_summary_markup((bot.meeting_summary or "").strip())
    if not cleaned_summary:
        raise MeetingSummaryError("Meeting summary not found")
    return _build_summary_docx(bot, cleaned_summary)


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


def _build_summary_docx(bot, summary_text):
    buffer = BytesIO()
    created_at_utc = timezone.now()
    generated_at = timezone.localtime(created_at_utc)
    created_at = created_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as docx_file:
        docx_file.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>""",
        )
        docx_file.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>""",
        )
        docx_file.writestr(
            "docProps/app.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
            xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Cursor</Application>
</Properties>""",
        )
        docx_file.writestr(
            "docProps/core.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
                   xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmlns:dcterms="http://purl.org/dc/terms/"
                   xmlns:dcmitype="http://purl.org/dc/dcmitype/"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>{escape(bot.name or "Meeting Summary")}</dc:title>
  <dc:creator>Cursor</dc:creator>
  <cp:lastModifiedBy>Cursor</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{created_at}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{created_at}</dcterms:modified>
</cp:coreProperties>""",
        )
        docx_file.writestr("word/styles.xml", _build_docx_styles_xml())
        docx_file.writestr(
            "word/_rels/document.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>""",
        )
        docx_file.writestr("word/document.xml", _build_docx_document_xml(bot, summary_text, generated_at))

    return buffer.getvalue()


def _build_docx_styles_xml():
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
    <w:rPr>
      <w:sz w:val="22"/>
      <w:szCs w:val="22"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="120"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle">
    <w:name w:val="Subtitle"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:after="40"/></w:pPr>
    <w:rPr><w:color w:val="6B7280"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:before="160" w:after="80"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:before="140" w:after="60"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:before="120" w:after="40"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Quote">
    <w:name w:val="Quote"/>
    <w:basedOn w:val="Normal"/>
    <w:qFormat/>
    <w:pPr>
      <w:ind w:left="720"/>
      <w:spacing w:after="80"/>
    </w:pPr>
    <w:rPr><w:i/><w:color w:val="4B5563"/></w:rPr>
  </w:style>
</w:styles>"""


def _build_docx_document_xml(bot, summary_text, generated_at):
    generated_at_display = generated_at.strftime("%Y-%m-%d %H:%M")
    body_elements = [
        _docx_paragraph_xml(bot.name or "Meeting Summary", style="Title"),
        _docx_paragraph_xml(f"Bot ID: {bot.object_id}", style="Subtitle"),
        _docx_paragraph_xml(f"Generated: {generated_at_display}", style="Subtitle"),
    ]
    body_elements.extend(_build_docx_body_elements(summary_text))
    body_xml = "".join(body_elements) or _docx_paragraph_xml("No summary content available.")
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas"
            xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
            xmlns:o="urn:schemas-microsoft-com:office:office"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"
            xmlns:v="urn:schemas-microsoft-com:vml"
            xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
            xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
            xmlns:w10="urn:schemas-microsoft-com:office:word"
            xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
            xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"
            xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk"
            xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml"
            xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
            mc:Ignorable="w14 wp14">
  <w:body>
    {body_xml}
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>"""


def _build_docx_body_elements(summary_text):
    elements = []
    paragraph_lines = []
    table_lines = []

    def flush_paragraph():
        if not paragraph_lines:
            return
        paragraph_text = " ".join(line.strip() for line in paragraph_lines).strip()
        paragraph_lines.clear()
        if paragraph_text:
            elements.append(_docx_paragraph_xml(paragraph_text))

    def flush_table():
        if not table_lines:
            return
        parsed_table = _parse_markdown_table(table_lines)
        table_lines.clear()
        if parsed_table:
            elements.append(_build_docx_table_xml(parsed_table))

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
            elements.append(_docx_paragraph_xml(line[4:], style="Heading3"))
            continue

        if line.startswith("## "):
            flush_paragraph()
            elements.append(_docx_paragraph_xml(line[3:], style="Heading2"))
            continue

        if line.startswith("# "):
            flush_paragraph()
            elements.append(_docx_paragraph_xml(line[2:], style="Heading1"))
            continue

        checklist_match = re.match(r"^[-*]\s+\[([ xX])\]\s+(.*)$", line)
        if checklist_match:
            flush_paragraph()
            checkbox = "[x]" if checklist_match.group(1).lower() == "x" else "[ ]"
            elements.append(_docx_paragraph_xml(f"{checkbox} {checklist_match.group(2)}"))
            continue

        bullet_match = re.match(r"^[-*]\s+(.*)$", line)
        if bullet_match:
            flush_paragraph()
            elements.append(_docx_paragraph_xml(f"- {bullet_match.group(1)}"))
            continue

        ordered_match = re.match(r"^(\d+)\.\s+(.*)$", line)
        if ordered_match:
            flush_paragraph()
            elements.append(_docx_paragraph_xml(f"{ordered_match.group(1)}. {ordered_match.group(2)}"))
            continue

        if line.startswith("> "):
            flush_paragraph()
            elements.append(_docx_paragraph_xml(line[2:], style="Quote"))
            continue

        paragraph_lines.append(line)

    flush_paragraph()
    flush_table()
    return elements


def _docx_paragraph_xml(text, style=None, bold=False):
    paragraph_properties = ""
    if style:
        paragraph_properties = f"<w:pPr><w:pStyle w:val=\"{style}\"/></w:pPr>"
    return f"<w:p>{paragraph_properties}{_docx_runs_xml(_strip_inline_markdown(text), bold=bold)}</w:p>"


def _docx_runs_xml(text, bold=False):
    parts = []
    for index, segment in enumerate((text or "").split("\n")):
        if index:
            parts.append("<w:r><w:br/></w:r>")
        run_props = "<w:rPr><w:b/></w:rPr>" if bold else ""
        parts.append(f"<w:r>{run_props}<w:t xml:space=\"preserve\">{escape(segment)}</w:t></w:r>")
    return "".join(parts) or "<w:r><w:t></w:t></w:r>"


def _strip_inline_markdown(text):
    stripped = text or ""
    stripped = re.sub(r"`([^`]+)`", r"\1", stripped)
    stripped = re.sub(r"\*\*([^*]+)\*\*", r"\1", stripped)
    stripped = re.sub(r"__([^_]+)__", r"\1", stripped)
    stripped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", stripped)
    stripped = re.sub(r"(?<!_)_([^_]+)_(?!_)", r"\1", stripped)
    return stripped


def _build_docx_table_xml(rows):
    row_xml = []
    for row_index, row in enumerate(rows):
        cells_xml = []
        for cell in row:
            cell_xml = _docx_paragraph_xml(cell or "-", bold=row_index == 0)
            cells_xml.append(
                f"""<w:tc>
  <w:tcPr>
    <w:tcW w:w="2400" w:type="dxa"/>
  </w:tcPr>
  {cell_xml}
</w:tc>"""
            )
        row_xml.append(f"<w:tr>{''.join(cells_xml)}</w:tr>")

    return f"""<w:tbl>
  <w:tblPr>
    <w:tblW w:w="0" w:type="auto"/>
    <w:tblBorders>
      <w:top w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>
      <w:left w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>
      <w:bottom w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>
      <w:right w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>
      <w:insideH w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>
      <w:insideV w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>
    </w:tblBorders>
  </w:tblPr>
  {''.join(row_xml)}
</w:tbl>"""


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
