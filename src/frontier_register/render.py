"""Excel and HTML/Confluence views of the same reviewed register state."""
from __future__ import annotations

from copy import copy
from datetime import date, datetime
from html import escape
from pathlib import Path
import re
from urllib.parse import urlsplit

from .core import HEADERS, DATE_FIELDS, SHEETS, fields_for, monitoring, digest

EXTRA_HEADERS = ["Evidence Monitoring Status", "Last Source Check UTC", "Record ID", "Bank Decision Evidence Version", "Benchmark Coverage", "Benchmark Evidence Review"]


def _excel_value(value, header):
    if value and header in DATE_FIELDS and isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    return value


def _write_cell(cell, value):
    cell.value = value
    # Literal string cell type prevents spreadsheet formula injection, including =HYPERLINK(...).
    if isinstance(value, str):
        cell.data_type = "s"


def export_excel(state: dict, template: Path, destination: Path) -> None:
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.table import TableColumn, Table, TableStyleInfo
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.utils import get_column_letter

    wb = load_workbook(template)
    navy, pale, amber = "173952", "F1F6FA", "FFF4D7"
    for sheet_name in SHEETS:
        ws = wb[sheet_name]
        records = [r for r in state["records"] if r["sheet"] == sheet_name]
        full_headers = HEADERS + EXTRA_HEADERS
        row_styles = [copy(ws.cell(5, c)._style) for c in range(1, 39)]
        for row in ws.iter_rows(min_row=5, max_row=max(ws.max_row, len(records)+4), max_col=len(full_headers)):
            for cell in row:
                cell.value = None
        for c, header in enumerate(full_headers, 1):
            cell = ws.cell(4, c)
            _write_cell(cell, header)
            cell.font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=navy)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for index, record in enumerate(records, 5):
            fields = fields_for(record)
            status, checked = monitoring(record, state)
            binding = record["metadata"].get("decision_vendor_sha256")
            from .benchmarks import iter_entries, monitoring as benchmark_monitoring, is_active
            entries = [e for _,e in iter_entries(state) if e.get("record_id") == record["record_id"] and is_active(state,e["data"]["benchmark_id"]) and e["review"]["status"] != "rejected"]
            coverage = f"{len(entries)} linked results; {sum(e['review']['status'] == 'accepted' for e in entries)} reviewed" if entries else "No linked published result; see Benchmark Coverage"
            evidence_status = benchmark_monitoring(record,state) or ("Linked evidence retained; review conditions" if entries else "Exact model/result correspondence not established")
            if "benchmarks" not in state:
                coverage,evidence_status="Benchmark evidence not initialized","Import evidence or run benchmark-refresh"
            values = [fields.get(h) for h in HEADERS] + [status, checked, record["record_id"], binding, coverage, evidence_status]
            for c, (header, value) in enumerate(zip(full_headers, values), 1):
                cell = ws.cell(index, c)
                if c <= 38:
                    cell._style = copy(row_styles[c-1])
                else:
                    cell.font = Font(name="Arial", size=11, color="243D50")
                    cell.fill = PatternFill("solid", fgColor=pale if index % 2 == 0 else "FFFFFF")
                _write_cell(cell, _excel_value(value, header))
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if header in DATE_FIELDS:
                    cell.number_format = "yyyy-mm-dd"
            if "required" in status or "unavailable" in status:
                ws.cell(index, 39).fill = PatternFill("solid", fgColor=amber)
            ws.row_dimensions[index].height = max(75, min(180, 14 * max(str(v or "").count("\n")+1 for v in values) + 18))
        ws.freeze_panes = "D5"
        ws.row_dimensions[4].height = 42
        for letter, width in (("AM", 55), ("AN", 27), ("AO", 40), ("AP", 68), ("AQ",48), ("AR",60)):
            ws.column_dimensions[letter].width = width
        table = next(iter(ws.tables.values()))
        table.ref = f"A4:{get_column_letter(len(full_headers))}{len(records)+4}"
        table.tableColumns = [TableColumn(id=i, name=h) for i, h in enumerate(full_headers, 1)]
        if table.autoFilter:
            table.autoFilter.ref = table.ref
        # Existing template contains only the two supported bank dropdowns.
        ws.data_validations.dataValidation = []
        for column, options in (("I", "Pending classification,Restricted,Eligible for assessment"),
                                ("AB", "Not determined,Within exclusion,Outside exclusion")):
            dv = DataValidation(type="list", formula1=f'"{options}"', allow_blank=False)
            ws.add_data_validation(dv)
            dv.add(f"{column}5:{column}{len(records)+4}")
        _write_cell(ws["A3"], f"State version {state['state_version']}. Updated {state['updated_at']}. Evidence monitoring is separate from the bank decision.")

    if "Source Monitor" in wb.sheetnames:
        del wb["Source Monitor"]
    monitor = wb.create_sheet("Source Monitor")
    monitor.sheet_view.showGridLines = False
    monitor.append(["Source monitoring and evidence provenance"])
    monitor.append(["A successful fetch confirms availability. It does not validate the meaning of a vendor claim."])
    monitor.append([])
    columns = ["Source ID", "URL", "Latest Check Status", "Checked At UTC", "Last Successful Content Fetch", "Last Confirmed UTC",
               "Text SHA256", "Raw SHA256", "Affected Records", "Extraction / Fetch Note"]
    monitor.append(columns)
    for sid, source in state["sources"].items():
        check = state["source_checks"].get(sid, {})
        vals = [sid, source["url"], check.get("status", "not_checked"), check.get("checked_at"), check.get("last_success_at"),
                check.get("last_confirmed_at"), check.get("text_sha256"), check.get("raw_sha256"),
                len(source.get("record_ids", [])), check.get("error") or check.get("extraction_warning")]
        monitor.append(vals)
        monitor.row_dimensions[monitor.max_row].height = 54
        for cell, value in zip(monitor[monitor.max_row], vals):
            _write_cell(cell, value)
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for sid,check in state.get("benchmarks",{}).get("source_checks",{}).items():
        vals = [sid,check.get("url") or check.get("source_url"),check.get("status"),check.get("checked_at") or check.get("retrieved_at"),
                check.get("last_success_at"),check.get("last_confirmed_at"),check.get("text_sha256"),check.get("raw_sha256") or check.get("source_sha256"),None,
                check.get("error") or check.get("notes") or check.get("extraction_warning")]
        monitor.append(vals)
        monitor.row_dimensions[monitor.max_row].height=54
        for cell,value in zip(monitor[monitor.max_row],vals):
            _write_cell(cell,value)
            cell.font=Font(name="Arial",size=10)
            cell.alignment=Alignment(vertical="top",wrap_text=True)
    for cell in monitor[4]:
        cell.font = Font(name="Arial", size=10, color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor=navy)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    monitor.row_dimensions[4].height = 34
    monitor.row_dimensions[1].height = 24
    monitor["A1"].font = Font(name="Arial", size=16, bold=True, color=navy)
    monitor["A2"].font = Font(name="Arial", size=10, italic=True, color="65748A")
    for c, width in enumerate((28, 70, 22, 25, 25, 25, 68, 68, 18, 55), 1):
        monitor.column_dimensions[get_column_letter(c)].width = width
    monitor.freeze_panes = "C5"
    if monitor.max_row > 4:
        table = Table(displayName="AutomationSourceMonitor", ref=f"A4:J{monitor.max_row}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        monitor.add_table(table)
    if "Field Guide" in wb.sheetnames:
        guide = wb["Field Guide"]
        extra = [
            ["Automation fields", "Source / Owner", "Meaning"],
            [EXTRA_HEADERS[0], "Automation", "Source changes, collection failures and decision-to-evidence drift are shown separately from bank classification."],
            [EXTRA_HEADERS[1], "Automation", "Latest attempted check, including failures. See Source Monitor for last success."],
            [EXTRA_HEADERS[2], "System identity", "Stable provider / exact-model / deployment key. Use it in review and bank-decision files."],
            [EXTRA_HEADERS[3], "Decision provenance", "Hash of vendor evidence fields at the recorded bank decision. A later difference triggers reassessment."],
            [EXTRA_HEADERS[4], "Published benchmark evidence", "Linked results are separate observations. See Benchmark Coverage for exact-ID joins, name/version suggestions and missing results."],
            [EXTRA_HEADERS[5], "Benchmark review", "New or corrected evidence requires review. Bank decisions additionally bind to the reviewed benchmark evidence and conditional scope."],
            ["Editing rule", "Operating process", "JSON state is the source of truth after initialization. Apply review or bank-decision files; direct Excel edits are not automatically re-imported."],
        ]
        for row_index, vals in enumerate(extra, 65):
            for col_index, val in enumerate(vals, 1):
                cell = guide.cell(row_index, col_index)
                _write_cell(cell, val)
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            guide.row_dimensions[row_index].height = 58
    if "benchmarks" in state:
        from .benchmark_views import write_excel_tables
        write_excel_tables(wb,state,_write_cell)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.tmp.xlsx")
    wb.save(temporary)
    wb.close()
    temporary.replace(destination)


def html_value(value) -> str:
    if value is None or value == "":
        return "—"
    output = []
    for line in str(value).splitlines():
        parsed = urlsplit(line) if line.startswith("https://") else None
        if parsed and parsed.hostname and not parsed.username and not parsed.password:
            output.append(f'<a href="{escape(line, quote=True)}">{escape(line)}</a>')
        else:
            output.append(escape(line))
    return "<br />".join(output)


def _table(headers: list[str], rows: list[list]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>"+"".join(f"<td>{html_value(v)}</td>" for v in row)+"</tr>" for row in rows)
    return f"<table><tbody><tr>{head}</tr>{body}</tbody></table>"


def confluence_fragment(state: dict, include_prior: bool = True) -> str:
    current = [r for r in state["records"] if r["sheet"] == "Model Register"]
    prior = [r for r in state["records"] if r["sheet"] == "Prior and Special"]
    parts = ["<h1>Frontier model register</h1>",
             f"<p>State version {state['state_version']}. Updated {escape(state['updated_at'])}. {len(current)} current model records.</p>",
             "<p><strong>Vendor evidence and bank decisions are separate.</strong> Pending classification is not permission to onboard. Source changes and failures are shown alongside retained findings.</p>"]
    for provider in ("Anthropic", "OpenAI", "Google"):
        rows = []
        for record in current:
            f = fields_for(record)
            if f["Provider"] != provider:
                continue
            rows.append([f["Model Family"], f["Version"], f["Exact Model ID"], f["Native Cyber Finding"],
                         f["Evidence Basis"], f["Bank Classification"], f["Bank Frontier Determination"], monitoring(record, state)[0]])
        parts.extend([f"<h2>{provider}</h2>", _table(["Model", "Version", "Exact ID", "Vendor cyber finding", "Evidence basis", "Bank classification", "Bank frontier determination", "Monitoring"], rows)])
    from .benchmark_views import html_sections
    parts.extend(html_sections(state,_table))
    parts.append("<h2>Current-model evidence and decision details</h2>")
    # Plain storage-compatible tables avoid relying on app-specific scripts or macros.
    for record in current:
        f = fields_for(record)
        parts.append(f"<h3>{escape(str(f['Provider']))}: {escape(str(f['Exact Model ID']))}</h3>")
        details = [[h, f.get(h)] for h in HEADERS if h not in ("Provider", "Exact Model ID")]
        status, checked = monitoring(record, state)
        details += [["Evidence monitoring", status], ["Last source check UTC", checked], ["Record ID", record["record_id"]]]
        parts.append(_table(["Field", "Recorded value"], details))
    if include_prior:
        parts.append("<h2>Prior releases and special deployments</h2><p>Capability coverage is recorded per entry. Historical inventory is not an approval list.</p>")
        parts.append(_table(["Provider", "Model", "Version", "Exact ID", "Lifecycle", "Bank classification", "Evidence basis", "Monitoring"],
                            [[r["vendor"].get("Provider"), r["vendor"].get("Model Family"), r["vendor"].get("Version"), r["vendor"].get("Exact Model ID"),
                              r["vendor"].get("Lifecycle"), r["bank"].get("Bank Classification"), r["vendor"].get("Evidence Basis"), monitoring(r,state)[0]] for r in prior]))
    failed = [s for s in state["source_checks"].values() if s["status"] == "unavailable"]
    watches = [s for s in state["sources"].values() if not s.get("record_ids")]
    parts.append("<h2>Catalog and framework watches</h2>")
    parts.append(_table(["URL", "Latest status", "Last check UTC"],
                        [[s["url"],state["source_checks"].get(s["source_id"],{}).get("status","not_checked"),state["source_checks"].get(s["source_id"],{}).get("checked_at")] for s in watches]))
    if failed:
        parts.append("<h2>Unavailable sources</h2>")
        parts.append(_table(["URL", "Attempted UTC", "Last successful content fetch", "Error"],
                            [[s["url"],s["checked_at"],s.get("last_success_at"),s.get("error")] for s in failed]))
    return "\n".join(parts)


def export_html(fragment: str, destination: Path) -> None:
    css = "body{font:15px/1.5 Arial,sans-serif;color:#243d50;margin:32px}h1,h2,h3{color:#173952}table{border-collapse:collapse;width:100%;margin:16px 0 32px}th,td{border:1px solid #d9e2e9;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#173952;color:white}tr:nth-child(even){background:#f1f6fa}a{color:#145d92}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Frontier model register</title><style>{css}</style></head><body>{fragment}</body></html>', encoding="utf-8")
