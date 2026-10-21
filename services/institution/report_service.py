"""Reproducible PDF/CSV/JSON exports of an exact immutable snapshot."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4
from xml.sax.saxutils import escape

from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from app.config import settings
from app.institution_errors import fail
from models.institution.common import canonical, normalize
from services.shared.evidence_service import write_bytes

TEMPLATE_VERSION = "TT_INSTITUTION_REPORT_1"
CSV_HEADER = ["section", "record_id", "parent_id", "field", "value", "unit"]
FONT_LOCK = Lock()


def report_styles(manifest):
    styles = getSampleStyleSheet()
    # Validate every source character before producing a PDF with unreadable
    # replacement glyphs. Fonts are licensed deployment assets, never user URLs.
    text = canonical(manifest).decode("utf-8")
    non_ascii = {ord(char) for char in text if ord(char) > 127}
    if not settings.INSTITUTION_REPORT_FONT_PATH:
        if non_ascii:
            fail(503, "REPORT_FONT_NOT_CONFIGURED", "Commission a Unicode font covering the report's source text.")
        return styles
    try:
        path = Path(settings.INSTITUTION_REPORT_FONT_PATH)
        data = path.read_bytes()
        checksum = hashlib.sha256(data).hexdigest()
        if len(data) > 20 * 1024 * 1024 or checksum != settings.INSTITUTION_REPORT_FONT_SHA256:
            raise ValueError("Font differs from its reviewed checksum")
        name = "TerraTrust_" + checksum[:24]
        with FONT_LOCK:
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, io.BytesIO(data), shapable=True))
            font = pdfmetrics.getFont(name)
        if any(code not in font.face.charToGlyph for code in non_ascii):
            fail(503, "REPORT_FONT_GLYPHS_MISSING", "The commissioned font does not cover all report characters.")
        import uharfbuzz  # Required shaping dependency, including Indic scripts.
        for style in styles.byName.values():
            style.fontName, style.bulletFontName, style.shaping = name, name, 1
        return styles
    except __import__("app.institution_errors", fromlist=["InstitutionError"]).InstitutionError:
        raise
    except Exception:
        fail(503, "REPORT_FONT_NOT_CONFIGURED", "The reviewed Unicode font or shaping engine is unavailable.")


def spreadsheet_text(value):
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def report_rows(manifest):
    frozen, result = manifest["frozen_input"], manifest["result"]
    rows = []
    for row in frozen["emissions"] + frozen["mitigations"]:
        for field, value in normalize(row).items():
            if field in {"created_by", "created_at", "updated_at"}:
                continue
            rows.append(["INPUT", str(row["id"]), str(result["audit_id"]), field, value, row.get("unit", "")])
    for line in result["calculation_lines"]:
        for field, value in line.items():
            rows.append(["CALCULATION", line["id"], line.get("input_id") or "", field, value, line["contribution_unit"]])
    for fid, factor in frozen["factors"].items():
        for field, value in factor.items():
            if field not in {"author_id", "reviewer_id"}:
                rows.append(["FACTOR", fid, "", field, value, ""])
    for field, value in result.items():
        if field != "calculation_lines":
            rows.append(["TOTAL", result.get("id") or result["snapshot_id"], result["audit_id"], field, value, "tCO2e" if field in {"actual_total", "inventory_balance", "quantified_actual_subtotal"} else ""])
    for field in ("input_hash", "result_hash", "versions", "evidence_manifest", "coverage", "review", "warnings"):
        value = result.get(field, frozen.get(field, manifest.get(field)))
        rows.append(["PROVENANCE", result["snapshot_id"], result["audit_id"], field, value, ""])
    return rows


def render(manifest, format_name):
    manifest = normalize(manifest)
    if format_name == "JSON":
        return canonical(manifest), "application/json"
    if format_name == "CSV":
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(CSV_HEADER)
        for row in report_rows(manifest):
            writer.writerow([spreadsheet_text(canonical(value).decode() if isinstance(value, (dict, list)) else "" if value is None else value) for value in row])
        return output.getvalue().encode("utf-8-sig"), "text/csv"
    if format_name != "PDF":
        raise ValueError("Unsupported export format.")
    output = io.BytesIO()
    styles = report_styles(manifest)
    story = [Paragraph("TerraTrust institution assessment", styles["Title"])]
    if manifest["draft_watermark"]:
        story.append(Paragraph("DRAFT — provisional, independently unfinalized assessment", styles["Heading2"]))
    if manifest["data_origin"] == "SYNTHETIC" or manifest.get("demo"):
        story.append(Paragraph("DEMONSTRATION — no certification or tradable credits", styles["Heading2"]))
    story.append(Paragraph("Partial operational inventory. Standing stock, atmospheric change and avoided impacts are separate. This assessment is not a carbon neutrality certification.", styles["BodyText"]))
    for warning in manifest.get("warnings",[]):
        story.append(Paragraph(escape(warning),styles["BodyText"]))
    for section, rid, parent, field, value, unit in report_rows(manifest):
        rendered = canonical(value).decode() if isinstance(value, (dict, list)) else "Unavailable" if value is None else str(value)
        text = f"{section} · {field}: {rendered} {unit}"
        # Reportlab Paragraph takes markup; escape all source values first.
        story.append(Paragraph(escape(text), styles["BodyText"]))
        story.append(Spacer(1, 3))
    if manifest.get("advice"):
        story.append(Paragraph("AI advice — separately labeled, exact saved answer versions", styles["Heading2"]))
        for advice in manifest["advice"]:
            story.append(Paragraph(escape(canonical(advice).decode()), styles["BodyText"]))
    SimpleDocTemplate(output, pagesize=A4, title="TerraTrust institution assessment", author="TerraTrust", invariant=1).build(story)
    return output.getvalue(), "application/pdf"


async def artifact(repo, iid, parent_type, parent_id, data, mime, *, job_id=None, snapshot_id=None, draft=False):
    if len(data) > settings.INSTITUTION_DOWNLOAD_MAX_BYTES:
        fail(413, "REPORT_TOO_LARGE", "Report exceeds the supported 20 MiB download size.")
    if not settings.REPORT_STORAGE_BUCKET:
        fail(503, "STORAGE_NOT_CONFIGURED", "Private report storage is not commissioned.")
    aid = str(uuid4())
    hashed = hashlib.sha256(data).hexdigest()
    key = f"verified/{iid}/{parent_type.lower()}/{parent_id}/{hashed}"
    await write_bytes(settings.REPORT_STORAGE_BUCKET, key, data, mime)
    return await repo.insert("institution_artifacts", {"id": aid, "institution_id": iid, "parent_type": parent_type, "parent_id": parent_id, "producer_job_id": job_id, "snapshot_id": snapshot_id, "private_bucket": settings.REPORT_STORAGE_BUCKET, "object_key": key, "byte_hash": hashed, "size_bytes": len(data), "mime_type": mime, "status": "READY", "draft": draft})
