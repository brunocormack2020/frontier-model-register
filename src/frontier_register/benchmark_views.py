"""Plain-data tables used by Excel and Confluence benchmark views."""
from __future__ import annotations

import json
import math
from .benchmarks import REGISTRY, BY_ID, CORE, iter_entries, candidate_matches, is_active, summary


def text_json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True) if value else None


def scope_label(state, benchmark_id):
    scope = BY_ID[benchmark_id]["scope"]
    if scope in ("core","evidence"):
        return "Core" if scope == "core" else "Separate evidence"
    return f"{scope.title()} — enabled" if is_active(state,benchmark_id) else f"{scope.title()} — conditional, not enabled"


def score_display(data):
    value,unit = data["value"],data["unit"]
    if unit == "percent":
        return f"{value:g}%"
    if unit == "fraction":
        return f"{value:.4g} (fraction)"
    return f"{value:g} {unit}"


def mapping_label(entry):
    return {"exact_id":"Exact published ID","reviewed_label":"Reviewed label; snapshot unconfirmed","unmapped":"Unmapped reported model"}[entry["mapping_status"]]


def linked_id(state,entry):
    return next((r["vendor"]["Exact Model ID"] for r in state["records"] if r["record_id"] == entry.get("record_id")),None)


def review_label(entry):
    return entry["review"]["status"] + ("; source changed — review required" if entry.get("source_review_required") else "")


def ordered_entries(state,kind="benchmark"):
    order = {b["id"]:i for i,b in enumerate(REGISTRY)}
    return sorted((e for _,e in iter_entries(state,kind)),key=lambda e:(order[e["data"]["benchmark_id"]],e["data"]["provider"],e["data"]["reported_model"],e["data"]["metric"],e["data"]["source_result_id"]))


def _brief(value, limit):
    """Bound preview context only; original values remain in the detail tables."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit-1].rstrip() + "…"


def _preview_context(data):
    parts = []
    if data.get("scenario"):
        parts.append("Scenario: " + _brief(data["scenario"], 52))
    elif data.get("setting"):
        parts.append(_brief(data["setting"], 52))
    if data.get("agent"):
        parts.append(_brief(data["agent"], 28))
    budget = data.get("budget")
    if budget not in (None, "", {}, []):
        label = budget.get("reported_limit") if isinstance(budget, dict) else None
        if label is None:
            label = budget if isinstance(budget, str) else json.dumps(budget, ensure_ascii=False, sort_keys=True)
        parts.append(_brief(str(label), 48))
    if not parts and data.get("dataset_subset"):
        parts.append(_brief(data["dataset_subset"], 92))
    return _brief("; ".join(dict.fromkeys(parts)), 112) or "Configuration not reported; see details"


def _preview_mapping(item):
    if item["candidate"]:
        return "Unconfirmed name/version match"
    return {"exact_id": "Exact ID match", "reviewed_label": "Reviewed label; snapshot unconfirmed",
            "unmapped": "Unconfirmed mapping"}[item["entry"]["mapping_status"]]


def _preview_review(entry):
    if entry.get("source_review_required"):
        return "Source changed; review required"
    return {"pending": "Pending review", "accepted": "Accepted evidence", "rejected": "Rejected"}[entry["review"]["status"]]


def coverage_index(state):
    """Read-only view joins, including explicitly unconfirmed name suggestions."""
    indexed = {}
    records = {r["record_id"]: r for r in state["records"]}
    for kind, sheet in (("benchmark", "Benchmark Results"), ("aisi", "AISI Evidence")):
        for row, entry in enumerate(ordered_entries(state, kind), 5):
            if entry["review"]["status"] == "rejected":
                continue
            data = entry["data"]
            rid = entry.get("record_id")
            targets = [rid] if rid in records else []
            candidate = not rid
            if candidate:
                for match in candidate_matches(state, data):
                    # A disclosed contradictory ID cannot become a name suggestion.
                    # This is a display guard, not a change to mapping/review state.
                    disclosed = data.get("model_id")
                    registered = records[match["record_id"]]["vendor"]["Exact Model ID"]
                    if not disclosed or disclosed == registered:
                        targets.append(match["record_id"])
            for target in targets:
                indexed.setdefault((target, data["benchmark_id"]), []).append({
                    "entry": entry, "candidate": candidate, "sheet": sheet, "row": row})
    return indexed


def coverage_preview(state, record, benchmark_id, indexed=None):
    """Return a bounded score preview and its first internal detail destination.

    Values are not aggregated across metrics, units, budgets or configurations.
    Candidate scores and scope-off scores are reference views only: this function
    never writes model links, review decisions or selected scope back to state.
    """
    if indexed is None:
        indexed = coverage_index(state)
    items = indexed.get((record["record_id"], benchmark_id), [])
    items = sorted(items, key=lambda item: (item["candidate"], item["row"]))
    failed = any(c.get("benchmark_id") == benchmark_id and c.get("status") in
                 ("unavailable", "error", "parse_error")
                 for c in state.get("benchmarks", {}).get("source_checks", {}).values())
    failed = failed or any(item["entry"].get("source_watch", {}).get("status") in
                           ("unavailable", "error", "parse_error") for item in items)
    lines = []
    if not is_active(state, benchmark_id):
        lines.append("Scope off; reference only")
    if not items:
        lines.append("Source unavailable; no linked result" if failed else "No linked published result")
        return {"text": "\n".join(lines), "location": None, "observation_ids": []}
    if failed:
        lines.append("Source unavailable; retained values may be stale")
    if any(item["entry"].get("source_review_required") for item in items):
        lines.append("Source changed; review required")
    linked = sum(not item["candidate"] for item in items)
    unconfirmed = len(items) - linked
    if linked and unconfirmed:
        lines.append(f"{linked} linked, {unconfirmed} unconfirmed observations")
    common_mapping = {_preview_mapping(item) for item in items}
    common_review = {_preview_review(item["entry"]) for item in items}
    common_model = {item["entry"]["data"]["reported_model"] for item in items}
    if len(common_mapping) == 1:
        lines.append(next(iter(common_mapping)))
    if len(common_review) == 1:
        review = next(iter(common_review))
        if review not in lines:
            lines.append(review)
    if len(common_model) == 1:
        lines.append("Reported: " + _brief(next(iter(common_model)), 56))
    shown = items[:3]
    # When both kinds exist, expose at least one unconfirmed preview as well as
    # the counts. This does not select a best result or imply comparability.
    if linked and unconfirmed and not any(item["candidate"] for item in shown):
        shown[-1] = next(item for item in items if item["candidate"])
    for item in shown:
        entry, data = item["entry"], item["entry"]["data"]
        if len(common_mapping) != 1:
            lines.append(_preview_mapping(item))
        if len(common_review) != 1:
            lines.append(_preview_review(entry))
        if len(common_model) != 1:
            lines.append("Reported: " + _brief(data["reported_model"], 56))
        score = score_display(data)
        if data.get("conditions", {}).get("reported_counts_are_approximate"):
            score += " (approximate)"
        lines.append(f"{score} — {_brief(data['metric'], 68)} [row {item['row']}]")
        lines.append(_preview_context(data))
    if len(shown) < len(items):
        lines.append(f"Showing {len(shown)} of {len(items)}; see details")
    lines.append("Click for " + shown[0]["sheet"])
    first = shown[0]
    return {"text": "\n".join(lines), "location": f"'{first['sheet']}'!A{first['row']}",
            "observation_ids": [item["entry"]["observation_id"] for item in shown]}


def workbook_tables(state):
    ids = CORE + [b["id"] for b in REGISTRY if b["scope"] in ("coding","soc")] + ["aisi_cyber_range"]
    indexed = coverage_index(state)
    coverage_rows, coverage_links = [], {}
    for row, record in enumerate(state["records"], 5):
        previews = [coverage_preview(state, record, bid, indexed) for bid in ids]
        coverage_rows.append([record["vendor"]["Provider"], record["vendor"]["Exact Model ID"], record["sheet"],
                              *[p["text"] for p in previews], record["record_id"]])
        for column, preview in enumerate(previews, 4):
            if preview["location"]:
                coverage_links[(row, column)] = preview["location"]
    columns = ["Provider","Reported Model","Benchmark","Metric","Published Score","Source Unit","Tasks Evaluated","Attempts",
        "Agent / Scaffold","Setting","Evaluation Date","Published / Leaderboard Date","Review Status","Model Mapping","Linked Model ID","Scope Selection",
        "Benchmark Version","Dataset / Subset","Successes","Budget","Model Safeguards","Target Mitigations","Additional Conditions",
        "Evaluator","Source URL","Retrieved UTC","Source SHA256","Source Result ID","Observation ID","Review Note","Source Revision","Previous Revisions","Notes"]
    rows, percent_rows = [], []
    for entry in ordered_entries(state):
        d = entry["data"]
        value = d["value"] / 100 if d["unit"] == "percent" else d["value"]
        if d["unit"] == "percent":
            percent_rows.append(len(rows)+5)
        rows.append([d["provider"],d["reported_model"],BY_ID[d["benchmark_id"]]["name"],d["metric"],value,d["unit"],d.get("total_tasks"),d.get("attempts"),
            d.get("agent"),d.get("setting"),d.get("evaluation_date"),d.get("published_date"),review_label(entry),mapping_label(entry),linked_id(state,entry),scope_label(state,d["benchmark_id"]),
            d.get("benchmark_version"),d.get("dataset_subset"),d.get("successes"),text_json(d.get("budget")),d.get("safeguards"),d.get("mitigations"),text_json(d.get("conditions")),
            d.get("evaluator"),d["source_url"],d.get("retrieved_at"),d.get("source_sha256"),d["source_result_id"],entry["observation_id"],entry["review"].get("note"),d.get("source_revision"),len(entry.get("history",[])),d.get("notes")])
    aisi_rows = []
    for entry in ordered_entries(state,"aisi"):
        d = entry["data"]
        aisi_rows.append([d["provider"],d["reported_model"],d["scenario"],d["metric"],d["value"],d["unit"],d.get("scenario_steps") or d.get("total_tasks"),d.get("repeats"),text_json(d.get("budget")),
            "No" if d.get("active_defenders") is False else "Yes" if d.get("active_defenders") is True else "Not reported",d.get("safeguards"),review_label(entry),mapping_label(entry),linked_id(state,entry),
            d.get("evaluation_date"),d.get("published_date"),d.get("evaluator"),d["source_url"],d.get("source_sha256"),entry["observation_id"],d.get("notes"),text_json(d.get("conditions")),entry["review"].get("note")])
    guide = [[b["name"],b["category"],scope_label(state,b["id"]),b["metrics"],b["direction"],b["limit"],b["url"],b["id"]] for b in REGISTRY]
    return [
        {"sheet":"Benchmark Coverage","title":"Published scores by registered model","note":"Up to 3 observations per cell. Click a populated cell for the first detail row; filter the detail sheet for all results. Unconfirmed matches require model review. Missing results are not zero.",
         "headers":["Provider","Registered Model ID","Register Section",*[BY_ID[x]["name"] for x in ids],"Record ID"],"rows":coverage_rows,
         "widths":[16,36,23]+[44]*len(ids)+[40],"row_height":72,"freeze":"D5","internal_links":coverage_links},
        {"sheet":"Benchmark Results","title":"Published benchmark results","note":"Each row is one metric under a reported test configuration. Pending results are displayed as published, not accepted bank evidence. Percentages are numeric Excel percentages.",
         "headers":columns,"rows":rows,"widths":[16,32,30,32,18,16,17,12,32,55,19,22,17,36,36,34,32,45,15,55,55,45,70,40,75,26,68,70,40,65,45,18,90],
         "row_height":72,"freeze":"C5","score_column":5,"percent_rows":percent_rows},
        {"sheet":"AISI Evidence","title":"AISI scenario-specific cyber-range evidence","note":"Mean progress, best-run progress and full success are separate metrics. These studies do not supply a universal AISI score. Model snapshot mapping remains explicit.",
         "headers":["Provider","Reported Model","Scenario","Metric","Published Value","Unit","Scenario Steps / Tasks","Repeated Runs","Budget","Active Defenders","Model Safeguards","Review Status","Model Mapping","Linked Model ID","Evaluation Date","Publication Date","Evaluator","Source URL","Source SHA256","Observation ID","Notes","Additional Conditions","Review Note"],
         "rows":aisi_rows,"widths":[16,32,35,34,19,18,21,18,48,20,55,18,37,36,19,19,22,80,68,40,100,70,65],"row_height":72,"freeze":"D5","score_column":5},
        {"sheet":"Benchmark Guide","title":"Benchmark definitions and selected scope","note":"Core benchmarks are always selected. Coding and SOC tracks are conditional. Scores stay separate from the bank's frontier determination.",
         "headers":["Benchmark","Capability / Property","Scope Selection","Published Metrics","Interpretation","Comparison Limits","Official Source","Benchmark ID"],"rows":guide,
         "widths":[35,38,35,65,55,95,80,35],"row_height":96,"freeze":"B5"},
    ]


def write_excel_tables(wb,state,write_cell):
    from openpyxl.styles import Alignment,Font,PatternFill
    from openpyxl.worksheet.table import Table,TableStyleInfo
    from openpyxl.utils import get_column_letter
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.worksheet.hyperlink import Hyperlink
    from datetime import date
    for spec in workbook_tables(state):
        if spec["sheet"] in wb.sheetnames:
            del wb[spec["sheet"]]
        ws = wb.create_sheet(spec["sheet"])
        ws.sheet_view.showGridLines = False
        write_cell(ws.cell(2,1),spec["title"])
        ws.cell(2,1).font = Font(name="Arial",size=16,bold=True,color="173952")
        ws.row_dimensions[2].height = 26
        write_cell(ws.cell(3,1),spec["note"])
        ws.cell(3,1).font = Font(name="Arial",size=10,italic=True,color="65748A")
        ws.row_dimensions[3].height = 23
        for c,h in enumerate(spec["headers"],1):
            cell=ws.cell(4,c)
            write_cell(cell,h)
            cell.font=Font(name="Arial",size=10,bold=True,color="FFFFFF")
            cell.fill=PatternFill("solid",fgColor="173952")
            cell.alignment=Alignment(horizontal="center",vertical="center",wrap_text=True)
        ws.row_dimensions[4].height=42
        for r,values in enumerate(spec["rows"],5):
            for c,value in enumerate(values,1):
                header=spec["headers"][c-1]
                if header in ("Evaluation Date","Publication Date","Published / Leaderboard Date") and isinstance(value,str) and len(value)==10:
                    try:value=date.fromisoformat(value)
                    except ValueError:pass
                cell=ws.cell(r,c)
                write_cell(cell,value)
                cell.font=Font(name="Arial",size=10,color="243D50")
                cell.fill=PatternFill("solid",fgColor="F1F6FA" if r%2==0 else "FFFFFF")
                cell.alignment=Alignment(vertical="top",wrap_text=True,horizontal="right" if isinstance(value,(int,float)) else "left")
                if isinstance(value,date):cell.number_format="yyyy-mm-dd"
                location = spec.get("internal_links", {}).get((r, c))
                if location:
                    cell.hyperlink = Hyperlink(ref=cell.coordinate, location=location,
                                              tooltip="Open the first displayed observation; filter the detail sheet for all matching results.")
                    cell.font=Font(name="Arial",size=10,color="145B83")
            lines=max((sum(max(1,math.ceil(len(line)/max(8,spec['widths'][i]*0.9))) for line in str(value or '').splitlines()) for i,value in enumerate(values)),default=1)
            ws.row_dimensions[r].height=min(400,max(spec["row_height"],lines*12+12))
        for c,width in enumerate(spec["widths"],1):
            ws.column_dimensions[get_column_letter(c)].width=width
        if spec.get("score_column"):
            for r in range(5,ws.max_row+1):ws.cell(r,spec["score_column"]).number_format="General"
            for r in spec.get("percent_rows",[]):ws.cell(r,spec["score_column"]).number_format="0.00%"
        ws.freeze_panes=spec["freeze"]
        if spec["rows"]:
            tab=Table(displayName="Published"+spec["sheet"].replace(" ",""),ref=f"A4:{get_column_letter(len(spec['headers']))}{ws.max_row}")
            tab.tableStyleInfo=TableStyleInfo(name="TableStyleMedium2",showRowStripes=True)
            ws.add_table(tab)
            if "Review Status" in spec["headers"]:
                letter=get_column_letter(spec["headers"].index("Review Status")+1)
                ws.conditional_formatting.add(f"{letter}5:{letter}{ws.max_row}",FormulaRule(formula=[f'{letter}5="pending"'],fill=PatternFill("solid",fgColor="FFF4D7")))
    # Keep the current inventory and its coverage together, preserving other relative order.
    sheet=wb["Benchmark Coverage"]
    wb.move_sheet(sheet,offset=-(wb.sheetnames.index("Benchmark Coverage")-1))


def html_sections(state,table):
    if "benchmarks" not in state:
        return []
    from html import escape
    stats=summary(state)
    scope=state["benchmarks"]["scope"]
    parts=["<h2>Published cyber benchmark evidence</h2>",
        f"<p>{stats['benchmark_observations']} benchmark observations; {stats['aisi_findings']} AISI findings. Coding tracks: {'enabled' if scope['coding'] else 'conditional, not enabled'}. SOC tracks: {'enabled' if scope['soc'] else 'conditional, not enabled'}.</p>",
        "<p>Each score belongs to its reported model, agent and evaluation configuration. Pending evidence is displayed with its review status. Missing results are not zero. Exact checkpoint correspondence is not inferred from a family name.</p>"]
    for b in REGISTRY:
        if b["id"]=="aisi_cyber_range" or not is_active(state,b["id"]):continue
        entries=[e for e in ordered_entries(state) if e["data"]["benchmark_id"]==b["id"]]
        parts += [f"<h3>{escape(b['name'])}</h3>",f"<p>{escape(b['limit'])}</p>"]
        if not entries:
            parts.append("<p>No published results collected for this track.</p>")
            continue
        rows=[]
        for e in entries:
            d=e["data"]
            context="; ".join(str(v) for v in [d.get("setting"),d.get("benchmark_version"),d.get("dataset_subset"),text_json(d.get("budget")),d.get("safeguards"),d.get("mitigations")] if v)
            rows.append([d["provider"]+": "+d["reported_model"],d.get("agent"),d["metric"],score_display(d),d.get("total_tasks"),
                context,d.get("evaluation_date"),d.get("published_date"),review_label(e)+"; "+mapping_label(e),d["source_url"]])
        parts.append(table(["Reported model","Agent","Metric","Score","Tasks","Evaluation conditions","Evaluated","Published / leaderboard date","Review / mapping","Source"],rows))
    parts += ["<h3>AISI cyber-range findings</h3>","<p>Interpret progress and success within the named scenario, budget and study. Published historical ranges had no active defenders. A missing provider/model result is a coverage gap.</p>"]
    parts.append(table(["Reported model","Scenario","Metric","Value","Runs","Budget","Evaluated","Published","Review / mapping","Source"],
        [[e["data"]["provider"]+": "+e["data"]["reported_model"],e["data"]["scenario"],e["data"]["metric"],score_display(e["data"]),e["data"].get("repeats"),text_json(e["data"].get("budget")),e["data"].get("evaluation_date"),e["data"].get("published_date"),review_label(e)+"; "+mapping_label(e),e["data"]["source_url"]] for e in ordered_entries(state,"aisi")]))
    failed=[c for c in state["benchmarks"]["source_checks"].values() if c.get("status") in ("unavailable","error","parse_error")]
    if failed:
        parts += ["<h3>Benchmark collection issues</h3>",table(["Benchmark","URL","Status","Detail"],[[c.get("benchmark_id"),c.get("url") or c.get("source_url"),c.get("status"),c.get("error")] for c in failed])]
    return parts
