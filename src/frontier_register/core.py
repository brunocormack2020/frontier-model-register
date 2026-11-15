"""Register state, provenance, review transactions and bank-owned decisions.

Vendor findings are never interpreted into bank approval by this module.
State changes are atomic. Use StateLock around a load/modify/save transaction.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
import tempfile
import uuid
from contextlib import AbstractContextManager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HEADERS = [
    "Provider", "Model Usage", "Model Family", "Version", "Vendor-stated Strengths",
    "Exact Model ID", "Lifecycle", "Access / Deployment Notes", "Bank Classification",
    "Official Sources (URL)", "Inventory Verified On", "Bank Host / Endpoint / Region",
    "Bank Access / Safeguard Tier", "Framework / Version / Effective Date",
    "Risk Domains / Coverage", "Vendor Frontier Position", "Native Cyber Finding",
    "Other Native Findings", "Threshold Meaning / Reference", "Evidence Basis",
    "Assessed Model / Revision", "Evidence Date / Precision", "Primary Evidence (URL)",
    "Additional Evidence (URL)", "Evaluation Conditions", "Evidence Gaps / Coverage",
    "Vendor Safeguards / Access", "Bank Frontier Determination", "Bank Use / Data / Tools",
    "Bank Rationale / Open Decision", "Decision Owner", "Decision Date", "Approval Record",
    "Review Triggers", "Next Review Date", "Original Suggested Treatment", "Evidence Reviewed On",
    "Framework Sources (URL)",
]
BANK_FIELDS = {
    "Bank Classification", "Bank Host / Endpoint / Region", "Bank Access / Safeguard Tier",
    "Bank Frontier Determination", "Bank Use / Data / Tools", "Bank Rationale / Open Decision",
    "Decision Owner", "Decision Date", "Approval Record", "Review Triggers", "Next Review Date",
}
IMMUTABLE_FIELDS = {"Provider", "Exact Model ID", "Original Suggested Treatment"}
VENDOR_FIELDS = set(HEADERS) - BANK_FIELDS
VENDOR_EDIT_FIELDS = VENDOR_FIELDS - IMMUTABLE_FIELDS
SOURCE_FIELDS = {"Official Sources (URL)", "Primary Evidence (URL)", "Additional Evidence (URL)", "Framework Sources (URL)"}
DATE_FIELDS = {"Inventory Verified On", "Evidence Date / Precision", "Decision Date", "Next Review Date", "Evidence Reviewed On"}
SHEETS = ("Model Register", "Prior and Special")
PROVIDERS = ("Anthropic", "OpenAI", "Google")
CLASSIFICATIONS = {"Pending classification", "Restricted", "Eligible for assessment"}
DETERMINATIONS = {"Not determined", "Within exclusion", "Outside exclusion"}


class RegisterError(ValueError):
    """An actionable validation or state-integrity error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegisterError(f"Cannot read valid JSON from {path.name}.") from exc


class StateLock(AbstractContextManager):
    """Exclusive single-host writer lock; never auto-break another run's lock."""
    def __init__(self, work_dir: Path):
        self.path = Path(work_dir) / ".register.lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RegisterError("Another run holds work/.register.lock. Check the process before removing a stale lock.") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "started_at": utc_now()}, stream)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.path.unlink(missing_ok=True)
        return False


def audit(state: dict, action: str, actor: str, details: dict) -> None:
    previous = state["audit"][-1]["event_sha256"] if state["audit"] else None
    event = {"at": utc_now(), "action": action, "actor": actor, "details": details, "previous_event_sha256": previous}
    event["event_sha256"] = digest(event)
    state["audit"].append(event)
    state["state_version"] += 1
    state["updated_at"] = event["at"]


def load_state(path: Path) -> dict:
    state = read_json(path)
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise RegisterError("Unsupported register schema. Expected schema_version 1.")
    if len({r["record_id"] for r in state["records"]}) != len(state["records"]):
        raise RegisterError("Duplicate record IDs in register state.")
    prior = None
    for event in state["audit"]:
        unsigned = {k: v for k, v in event.items() if k != "event_sha256"}
        if event.get("previous_event_sha256") != prior or digest(unsigned) != event.get("event_sha256"):
            raise RegisterError("Audit chain is inconsistent. Restore or investigate the state before proceeding.")
        prior = event["event_sha256"]
    return state


def save_state(path: Path, state: dict) -> None:
    # Local history is recovery evidence, not an independent tamper-proof audit service.
    history = path.parent / "history" / f"state_{state['state_version']:06d}_{digest(state)[:12]}.json"
    atomic_json(history, state)
    atomic_json(path, state)


def record_id(provider: str, model_id: str, deployment_key: str = "unassigned") -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"frontier-register:{provider}:{model_id}:{deployment_key}"))


def normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise RegisterError(f"Unsupported cell value type: {type(value).__name__}")


def import_workbook(path: Path) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=False)
    records = []
    try:
        for sheet_name in SHEETS:
            if sheet_name not in wb.sheetnames:
                raise RegisterError(f"Missing worksheet: {sheet_name}")
            ws = wb[sheet_name]
            headers = [c.value for c in next(ws.iter_rows(min_row=4, max_row=4, max_col=38))]
            # Accept the earlier name for this one field, without changing its meaning.
            if headers[14] == "Assessed Risk Domains":
                headers[14] = "Risk Domains / Coverage"
            if headers != HEADERS:
                raise RegisterError(f"Unexpected 38-column schema in {sheet_name}. Use the supplied register template.")
            for cells in ws.iter_rows(min_row=5, max_col=38):
                if all(c.value is None for c in cells):
                    continue
                if any(c.data_type == "f" for c in cells):
                    raise RegisterError("Import contains formulas in record fields. Convert the intended data to literal values first.")
                fields = dict(zip(HEADERS, (normalize_value(c.value) for c in cells)))
                provider, model = fields["Provider"], fields["Exact Model ID"]
                if provider not in PROVIDERS or not isinstance(model, str) or not model.strip():
                    raise RegisterError("Each record needs an allowed provider and an exact model identifier.")
                if fields["Bank Classification"] not in CLASSIFICATIONS or fields["Bank Frontier Determination"] not in DETERMINATIONS:
                    raise RegisterError("Unrecognized bank decision value in seed workbook.")
                records.append({
                    "record_id": record_id(provider, model), "sheet": sheet_name,
                    "deployment_key": "unassigned",
                    "vendor": {k: v for k, v in fields.items() if k in VENDOR_FIELDS},
                    "bank": {k: v for k, v in fields.items() if k in BANK_FIELDS},
                    "metadata": {"origin": "workbook_import", "reviewed_source_hashes": {}, "pending_sources": [],
                                 "discovery_review_required": False, "decision_vendor_sha256": None},
                })
    finally:
        wb.close()
    if not records or len({r["record_id"] for r in records}) != len(records):
        raise RegisterError("Workbook has no records or duplicates provider/model identities. Resolve deployments explicitly.")
    now = utc_now()
    state = {"schema_version": 1, "created_at": now, "updated_at": now, "state_version": 0,
             "seed_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(), "records": records,
             "sources": {}, "source_checks": {}, "discovery_runs": [], "audit": []}
    audit(state, "import_workbook", "operator", {"records": len(records), "seed_sha256": state["seed_sha256"]})
    return state


def fields_for(record: dict) -> dict:
    return {**record["vendor"], **record["bank"]}


def deployment_digest(record: dict) -> str:
    return digest({"deployment_key": record["deployment_key"], **{k: record["bank"].get(k) for k in
                   ("Bank Host / Endpoint / Region", "Bank Access / Safeguard Tier", "Bank Use / Data / Tools")}})


def catalog_digest(observation: dict | None) -> str:
    return digest({k: v for k, v in (observation or {}).items() if k != "checked_at"})


def source_id(url: str) -> str:
    return "src_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def collect_source_catalog(state: dict, watch_sources: list[dict], allowed_hosts: list[str], include_prior: bool = False) -> list[dict]:
    sources: dict[str, dict] = {}
    allowed = {h.lower() for h in allowed_hosts}
    def add(url: str, rid: str | None, kind: str):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise RegisterError(f"Source URL host is not an approved HTTPS host: {parsed.hostname or 'missing host'}")
        sid = source_id(url)
        entry = sources.setdefault(sid, {"source_id": sid, "url": url, "record_ids": [], "kinds": []})
        if rid and rid not in entry["record_ids"]:
            entry["record_ids"].append(rid)
        if kind not in entry["kinds"]:
            entry["kinds"].append(kind)
    for record in state["records"]:
        if record["sheet"] == "Prior and Special" and not include_prior:
            continue
        for key in sorted(SOURCE_FIELDS):
            for line in str(record["vendor"].get(key) or "").splitlines():
                line = line.strip()
                if line.startswith("https://"):
                    add(line, record["record_id"], key)
    for item in watch_sources:
        add(item["url"], None, item.get("kind", "catalog_watch"))
    state["sources"] = sources
    return list(sources.values())


def apply_fetch_results(state: dict, results: list[dict]) -> None:
    for result in results:
        sid = result["source_id"]
        state["source_checks"][sid] = result
        if result["status"] in ("new", "changed", "unchanged"):
            for record in state["records"]:
                if record["record_id"] in state["sources"].get(sid, {}).get("record_ids", []):
                    revision = digest({"raw": result.get("raw_sha256"), "text": result.get("text_sha256")})
                    if revision != record["metadata"]["reviewed_source_hashes"].get(sid):
                        record["metadata"]["pending_sources"] = sorted(set(record["metadata"]["pending_sources"]) | {sid})
    audit(state, "refresh_sources", "automation", {
        "counts": {status: sum(r["status"] == status for r in results) for status in ("new", "changed", "unchanged", "unavailable")},
        "source_ids": [r["source_id"] for r in results],
    })


def monitoring(record: dict, state: dict) -> tuple[str, str | None]:
    sids = [sid for sid, s in state["sources"].items() if record["record_id"] in s.get("record_ids", [])]
    checks = [state["source_checks"][sid] for sid in sids if sid in state["source_checks"]]
    last = max((c.get("checked_at", "") for c in checks), default=None)
    statuses = []
    if record["metadata"].get("discovery_review_required"):
        statuses.append("New model requires evidence review")
    if record["metadata"].get("pending_sources"):
        statuses.append("Reassessment required: source changes")
    if any(c["status"] == "unavailable" for c in checks):
        statuses.append("Source unavailable: retained findings may be stale")
    binding = record["metadata"].get("decision_vendor_sha256")
    if binding and binding != digest(record["vendor"]):
        statuses.append("Bank decision requires reassessment")
    scope_binding = record["metadata"].get("decision_deployment_sha256")
    catalog_binding = record["metadata"].get("decision_catalog_sha256")
    if (scope_binding and scope_binding != deployment_digest(record)) or (catalog_binding and catalog_binding != catalog_digest(record["metadata"].get("catalog_observation"))):
        statuses.append("Bank decision requires reassessment: deployment or catalog changed")
    if "benchmarks" in state:
        from .benchmarks import monitoring as benchmark_monitoring
        benchmark_status = benchmark_monitoring(record, state)
        if benchmark_status:
            statuses.append(benchmark_status)
    if not statuses:
        if not checks:
            statuses.append("Not checked by automation")
        elif len(checks) != len(sids):
            statuses.append("Some sources not checked")
        else:
            statuses.append("No pending monitored-source changes")
    return "; ".join(statuses), last


def make_review_queue(state: dict) -> dict:
    items = []
    for record in state["records"]:
        pending = record["metadata"].get("pending_sources", [])
        if not pending and not record["metadata"].get("discovery_review_required"):
            continue
        sources = []
        for sid in pending:
            check = state["source_checks"].get(sid, {})
            sources.append({"source_id": sid, "url": state["sources"].get(sid, {}).get("url"),
                            "status": check.get("status"), "text_sha256": check.get("text_sha256"),
                            "raw_sha256": check.get("raw_sha256"), "text_path": check.get("text_path"),
                            "raw_path": check.get("raw_path"), "checked_at": check.get("checked_at")})
        items.append({"record_id": record["record_id"], "provider": record["vendor"]["Provider"],
                      "model_id": record["vendor"]["Exact Model ID"], "action": "pending", "review_note": "",
                      "sources": sources, "catalog_observation": record["metadata"].get("catalog_observation"),
                      "raw_evidence_reviewed": False, "current_vendor_fields": record["vendor"], "proposed_updates": {}})
    return {"schema_version": 1, "state_sha256": digest(state), "generated_at": utc_now(),
            "instructions": "Set action to update or keep_existing, add review_note and proposed_updates. Bank fields are prohibited. Dates are explicit review inputs. Leave other items pending.",
            "items": items}


def _valid_field_value(key: str, value: Any) -> None:
    if value is not None and not isinstance(value, str):
        raise RegisterError(f"{key} must be a string or null.")
    if isinstance(value, str) and len(value) > 16000:
        raise RegisterError(f"{key} exceeds the 16000-character field limit.")
    if key in DATE_FIELDS and value:
        if key == "Evidence Date / Precision" and re.fullmatch(r"\d{4}-\d{2} \(month only\)", value):
            return
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise RegisterError(f"{key} needs YYYY-MM-DD, or month-only precision for Evidence Date / Precision.") from exc


def apply_review(state: dict, queue: dict, reviewer: str) -> dict:
    if not reviewer.strip():
        raise RegisterError("A reviewer attribution is required.")
    if queue.get("state_sha256") != digest(state):
        raise RegisterError("Review file is stale. Generate a new queue from the current state and reconcile the review.")
    result = copy.deepcopy(state)
    by_id = {r["record_id"]: r for r in result["records"]}
    seen, accepted = set(), []
    for item in queue.get("items", []):
        rid = item.get("record_id")
        if rid in seen or rid not in by_id:
            raise RegisterError("Review contains a duplicate or unknown record ID.")
        seen.add(rid)
        action = item.get("action", "pending")
        if action == "pending":
            continue
        if action not in ("update", "keep_existing") or not str(item.get("review_note", "")).strip():
            raise RegisterError("Accepted reviews require update/keep_existing and a review_note.")
        changes = item.get("proposed_updates", {})
        if not isinstance(changes, dict) or set(changes) - VENDOR_EDIT_FIELDS:
            raise RegisterError("Review attempts to change bank-owned, immutable or unknown fields.")
        if action == "keep_existing" and changes:
            raise RegisterError("keep_existing cannot include proposed_updates.")
        if action == "update" and not changes:
            raise RegisterError("update requires at least one proposed field change.")
        record = by_id[rid]
        expected = set(record["metadata"].get("pending_sources", []))
        supplied = [s.get("source_id") for s in item.get("sources", [])]
        if len(supplied) != len(set(supplied)) or set(supplied) != expected:
            raise RegisterError("Review must acknowledge the exact set of pending sources.")
        for source in item.get("sources", []):
            sid = source["source_id"]
            current = state["source_checks"].get(sid, {})
            if current.get("status") == "unavailable" or not current.get("raw_sha256") or source.get("text_sha256") != current.get("text_sha256") or source.get("raw_sha256") != current.get("raw_sha256"):
                raise RegisterError("A reviewed source is unavailable or its content hash no longer matches.")
            if (current.get("extraction_warning") or not current.get("text_sha256")) and item.get("raw_evidence_reviewed") is not True:
                raise RegisterError("Text extraction is incomplete. Review the raw document and explicitly set raw_evidence_reviewed to true.")
            record["metadata"]["reviewed_source_hashes"][sid] = digest({"raw": current.get("raw_sha256"), "text": current.get("text_sha256")})
        before = digest(record["vendor"])
        for key, value in changes.items():
            _valid_field_value(key, value)
            record["vendor"][key] = value
        record["metadata"]["pending_sources"] = []
        record["metadata"]["discovery_review_required"] = False
        record["metadata"]["last_reviewed_at"] = utc_now()
        record["metadata"]["last_reviewer"] = reviewer
        record["metadata"]["reviewed_catalog_sha256"] = catalog_digest(record["metadata"].get("catalog_observation"))
        accepted.append({"record_id": rid, "action": action, "fields": sorted(changes), "before_vendor_sha256": before,
                         "after_vendor_sha256": digest(record["vendor"]), "note": item["review_note"], "sources": item.get("sources", [])})
    if not accepted:
        raise RegisterError("No accepted review items. The state was not changed.")
    audit(result, "apply_vendor_review", reviewer, {"items": accepted})
    return result


def apply_bank_decisions(state: dict, request: dict, actor: str) -> dict:
    if not actor.strip() or request.get("state_sha256") != digest(state):
        raise RegisterError("Bank decision request is stale or actor attribution is missing.")
    result = copy.deepcopy(state)
    by_id = {r["record_id"]: r for r in result["records"]}
    seen, events = set(), []
    for item in request.get("items", []):
        rid, changes = item.get("record_id"), item.get("updates", {})
        if rid not in by_id or rid in seen or not changes or set(changes) - BANK_FIELDS:
            raise RegisterError("Bank update contains an unknown/duplicate record, no changes, or non-bank fields.")
        seen.add(rid)
        record = by_id[rid]
        for key, value in changes.items():
            _valid_field_value(key, value)
        proposed = {**record["bank"], **changes}
        confirm = item.get("confirm_decision") is True
        administrative = {"Bank Host / Endpoint / Region", "Bank Access / Safeguard Tier", "Bank Use / Data / Tools", "Next Review Date", "Review Triggers"}
        if set(changes) - administrative and not confirm:
            raise RegisterError("Decision fields require confirm_decision=true and a current expected_vendor_sha256.")
        if confirm and item.get("expected_vendor_sha256") != digest(record["vendor"]):
            raise RegisterError("Bank decision must explicitly confirm the current vendor evidence hash.")
        if confirm and "benchmarks" in state:
            from .benchmarks import evidence_digest
            if item.get("expected_benchmark_sha256") != evidence_digest(record,state):
                raise RegisterError("Bank decision must explicitly confirm the current benchmark evidence hash. Regenerate bank-template.")
        if proposed["Bank Classification"] not in CLASSIFICATIONS or proposed["Bank Frontier Determination"] not in DETERMINATIONS:
            raise RegisterError("Unknown bank classification or frontier determination.")
        if confirm and proposed["Bank Classification"] != "Pending classification":
            for key in ("Bank Rationale / Open Decision", "Decision Owner", "Decision Date", "Approval Record"):
                if not changes.get(key) or changes[key] in ("Unassigned", "To be specified"):
                    raise RegisterError(f"A recorded bank decision needs {key}.")
        if proposed["Bank Classification"] == "Eligible for assessment" and proposed["Bank Frontier Determination"] != "Outside exclusion":
            raise RegisterError("Eligible for assessment requires an explicit Outside exclusion determination.")
        previous = record["bank"]
        record["bank"] = proposed
        if confirm:
            record["metadata"]["decision_vendor_sha256"] = digest(record["vendor"])
            record["metadata"]["decision_deployment_sha256"] = deployment_digest(record)
            record["metadata"]["decision_catalog_sha256"] = catalog_digest(record["metadata"].get("catalog_observation"))
            if "benchmarks" in result:
                from .benchmarks import evidence_digest
                record["metadata"]["decision_benchmark_sha256"] = evidence_digest(record, result)
        events.append({"record_id": rid, "previous_bank_fields": previous, "new_bank_fields": proposed,
                       "vendor_sha256": digest(record["vendor"]), "monitoring_at_decision": monitoring(record, state)[0]})
    if not events:
        raise RegisterError("No bank decision items supplied.")
    audit(result, "record_bank_decisions", actor, {"items": events})
    return result


def merge_discovery(state: dict, provider: str, models: list[dict], allowed_prefixes: list[str]) -> int:
    if provider not in PROVIDERS:
        raise RegisterError("Only Anthropic, OpenAI and Google are supported.")
    existing = {(r["vendor"]["Provider"], r["vendor"]["Exact Model ID"]): r for r in state["records"]}
    added, changed = [], []
    for model in models:
        mid = model.get("model_id", "")
        if not isinstance(mid, str) or not mid.startswith(tuple(allowed_prefixes)):
            continue
        if (provider, mid) in existing:
            record = existing[(provider, mid)]
            old = record["metadata"].get("catalog_observation")
            if catalog_digest(old) != catalog_digest(model):
                if old:
                    record["metadata"].setdefault("catalog_history", []).append(old)
                record["metadata"]["discovery_review_required"] = True
                changed.append(record["record_id"])
            record["metadata"]["catalog_observation"] = model
            continue
        vendor = {k: None for k in VENDOR_FIELDS}
        vendor.update({"Provider": provider, "Exact Model ID": mid, "Model Family": model.get("display_name") or mid,
                       "Model Usage": "Unclassified (discovered)", "Version": "Resolve exact revision",
                       "Vendor-stated Strengths": (model.get("description") or "Catalog metadata only. Strengths require review.")[:1000],
                       "Lifecycle": "Observed in account-visible API catalog. Lifecycle not verified.",
                       "Evidence Basis": "API catalog metadata only. Capability evidence not reviewed.",
                       "Evidence Gaps / Coverage": "Obtain model-specific evidence, assess scope and resolve serving revision before classification.",
                       "Native Cyber Finding": "Not reviewed for this model", "Other Native Findings": "Not reviewed for this model",
                       "Original Suggested Treatment": "Discovered by automation. No original recommendation."})
        bank = {k: None for k in BANK_FIELDS}
        bank.update({"Bank Classification": "Pending classification", "Bank Frontier Determination": "Not determined",
                     "Decision Owner": "Unassigned", "Bank Rationale / Open Decision": "New catalog entry requires evidence and bank review."})
        rid = record_id(provider, mid)
        state["records"].append({"record_id": rid, "sheet": "Model Register", "deployment_key": "unassigned", "vendor": vendor, "bank": bank,
                                 "metadata": {"origin": "api_discovery", "catalog_observation": model, "reviewed_source_hashes": {},
                                              "pending_sources": [], "discovery_review_required": True, "decision_vendor_sha256": None}})
        added.append(rid)
        existing[(provider, mid)] = state["records"][-1]
    state["discovery_runs"].append({"provider": provider, "at": utc_now(), "seen": len(models), "added": len(added)})
    audit(state, "discover_models", "automation", {"provider": provider, "seen": len(models), "added_record_ids": added, "changed_catalog_record_ids": changed,
                                                 "absence_rule": "No retirement or removal inferred from absence."})
    return len(added)
