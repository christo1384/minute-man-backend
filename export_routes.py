"""
Minute Man — multi-format export utilities (FastAPI).

  POST /export/pdf   -> application/pdf download
  POST /export/excel -> .xlsx download

Both accept the same structured payload (MinutesExportRequest — the output of
/api/minutes) and render it into a clean, tabular document. They do NOT call the
LLM; they only format already-extracted structured data.
"""

import io
from datetime import date
from typing import List, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

router = APIRouter(prefix="/export", tags=["export"])

# Brand palette (matches the front-end theme)
SLATE_INDUSTRIAL = "2F4F4F"
SAFETY_ORANGE = "FF8C00"


# ---------------------------------------------------------------------------
# Shared schema — the single contract between engine, front-end, and exporter.
# ---------------------------------------------------------------------------
class HazardRow(BaseModel):
    hazard: str
    control: str
    control_tier: str
    compliance_note: str


class ActionRow(BaseModel):
    who: str
    what: str
    by_when: str


class AttendanceEntry(BaseModel):
    name: str
    signature: str = ""


class MinutesExportRequest(BaseModel):
    meeting_type: str
    site_name: Optional[str] = ""
    meeting_date: Optional[str] = Field(default_factory=lambda: date.today().isoformat())
    hazards: List[HazardRow] = []
    actions: List[ActionRow] = []
    decisions: List[str] = []
    attendance: List[AttendanceEntry] = []


DISCLAIMER = (
    "This record is AI-generated decision support based on a verbal transcript. "
    "It is not a certified legal or WorkSafe NZ compliance document. A responsible "
    "person must review, correct, and formally sign off this record before "
    "distribution or filing."
)


# ---------------------------------------------------------------------------
# PDF export (reportlab)
# ---------------------------------------------------------------------------
def _build_pdf(payload: MinutesExportRequest) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Minute Man - {payload.meeting_type}",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("MMTitle", parent=styles["Title"],
                                 textColor=colors.HexColor(f"#{SLATE_INDUSTRIAL}"), fontSize=18)
    heading_style = ParagraphStyle("MMHeading", parent=styles["Heading2"],
                                   textColor=colors.HexColor(f"#{SLATE_INDUSTRIAL}"), spaceBefore=14)
    meta_style = ParagraphStyle("MMMeta", parent=styles["Normal"], textColor=colors.grey)
    body_style = styles["Normal"]
    disclaimer_style = ParagraphStyle("MMDisclaimer", parent=styles["Normal"],
                                      fontSize=8, textColor=colors.grey, spaceBefore=16)

    elements = [
        Paragraph(f"Minute Man — {payload.meeting_type} Minutes", title_style),
        Paragraph(f"Site: {payload.site_name or 'Not specified'} &nbsp;|&nbsp; Date: {payload.meeting_date}", meta_style),
        Spacer(1, 10),
    ]
    header_fill = colors.HexColor(f"#{SLATE_INDUSTRIAL}")
    accent_fill = colors.HexColor(f"#{SAFETY_ORANGE}")

    def styled_table(rows, col_widths, fill):
        table = Table(rows, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), fill),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F5F5")]),
        ]))
        return table

    # 1. Hazards & Risk Controls
    elements.append(Paragraph("1. Hazards &amp; Risk Controls", heading_style))
    if payload.hazards:
        rows = [["Hazard Identified", "Control Discussed", "Hierarchy Tier", "HSWA Compliance Note"]]
        rows += [[h.hazard, h.control, h.control_tier, h.compliance_note] for h in payload.hazards]
        elements.append(styled_table(rows, [42 * mm, 42 * mm, 32 * mm, 42 * mm], header_fill))
    else:
        elements.append(Paragraph("No hazards recorded.", body_style))

    # 2. Action Register
    elements.append(Paragraph("2. Action Register (Who / What / When)", heading_style))
    if payload.actions:
        rows = [["Who", "What", "By When"]]
        rows += [[a.who, a.what, a.by_when] for a in payload.actions]
        elements.append(styled_table(rows, [38 * mm, 90 * mm, 30 * mm], accent_fill))
    else:
        elements.append(Paragraph("No actions recorded.", body_style))

    # 3. Decisions
    elements.append(Paragraph("3. Decisions Made", heading_style))
    if payload.decisions:
        for d in payload.decisions:
            elements.append(Paragraph(f"• {d}", body_style))
    else:
        elements.append(Paragraph("No formal decisions recorded.", body_style))

    # 4. Attendance
    elements.append(Paragraph("4. Attendance Record", heading_style))
    if payload.attendance:
        rows = [["Name", "Signature"]]
        rows += [[e.name, e.signature or "—"] for e in payload.attendance]
        elements.append(styled_table(rows, [80 * mm, 78 * mm], header_fill))
    else:
        elements.append(Paragraph("No attendance recorded.", body_style))

    elements.append(Paragraph(DISCLAIMER, disclaimer_style))
    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


@router.post("/pdf")
def export_pdf(payload: MinutesExportRequest):
    pdf_bytes = _build_pdf(payload)
    filename = f"minute-man-{payload.meeting_type.lower().replace(' ', '-')}-{payload.meeting_date}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Excel export (openpyxl)
# ---------------------------------------------------------------------------
def _autosize(ws, widths):
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width


def _build_excel(payload: MinutesExportRequest) -> bytes:
    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    fill_slate = PatternFill(start_color=SLATE_INDUSTRIAL, end_color=SLATE_INDUSTRIAL, fill_type="solid")
    fill_orange = PatternFill(start_color=SAFETY_ORANGE, end_color=SAFETY_ORANGE, fill_type="solid")
    wrap = Alignment(wrap_text=True, vertical="top")

    # Sheet 1: Hazards & Controls
    ws1 = wb.active
    ws1.title = "Hazards & Controls"
    ws1.append(["Hazard Identified", "Control Discussed", "Hierarchy of Controls Tier", "HSWA Compliance Note"])
    for cell in ws1[1]:
        cell.font = header_font
        cell.fill = fill_slate
    for h in payload.hazards:
        ws1.append([h.hazard, h.control, h.control_tier, h.compliance_note])
    for row in ws1.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = wrap
    _autosize(ws1, [32, 32, 26, 34])

    # Sheet 2: Action Register
    ws2 = wb.create_sheet("Action Register")
    ws2.append(["Who", "What", "By When"])
    for cell in ws2[1]:
        cell.font = header_font
        cell.fill = fill_orange
    for a in payload.actions:
        ws2.append([a.who, a.what, a.by_when])
    for row in ws2.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = wrap
    _autosize(ws2, [24, 60, 18])

    # Sheet 3: Decisions
    ws3 = wb.create_sheet("Decisions")
    ws3.append(["Decision"])
    for cell in ws3[1]:
        cell.font = header_font
        cell.fill = fill_slate
    if payload.decisions:
        for d in payload.decisions:
            ws3.append([d])
    else:
        ws3.append(["No formal decisions recorded."])
    _autosize(ws3, [80])

    # Sheet 4: Attendance
    ws4 = wb.create_sheet("Attendance Record")
    ws4.append(["Name", "Signature"])
    for cell in ws4[1]:
        cell.font = header_font
        cell.fill = fill_slate
    for e in payload.attendance:
        ws4.append([e.name, e.signature])
    _autosize(ws4, [30, 30])

    # Sheet 0: Summary cover
    ws0 = wb.create_sheet("Summary", 0)
    ws0.append(["Minute Man — Meeting Minutes"])
    ws0["A1"].font = Font(bold=True, size=14, color=SLATE_INDUSTRIAL)
    ws0.append(["Meeting Type", payload.meeting_type])
    ws0.append(["Site", payload.site_name or "Not specified"])
    ws0.append(["Date", payload.meeting_date])
    ws0.append([])
    ws0.append([DISCLAIMER])
    ws0["A6"].alignment = wrap
    _autosize(ws0, [90])
    wb.active = 0

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer.getvalue()


@router.post("/excel")
def export_excel(payload: MinutesExportRequest):
    xlsx_bytes = _build_excel(payload)
    filename = f"minute-man-{payload.meeting_type.lower().replace(' ', '-')}-{payload.meeting_date}.xlsx"
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
