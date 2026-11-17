"""Read-only Microsoft Foundry deployment inventory and change monitoring.

Azure ARM account deployments and project-visible deployments are distinct
observations. No discovery result is model approval, proof of model checkpoint,
application usage, processing geography, or deletion. No POST/PUT/DELETE calls.
"""
from __future__ import annotations

import copy
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, parse_qs

import httpx

from .intelligence import _atomic_json

SCHEMA_VERSION = 1
ARM_API_VERSION = "2025-06-01"
PROJECT_API_VERSION = "v1"
ARM_HOST = "management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
PROJECT_SCOPE = "https://ai.azure.com/.default"
_MAX_EVENTS = 2000
_MAX_HISTORY = 12
_ID = re.compile(r"^[a-zA-Z0-9_.-]{1,90}$")
_SUB = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ACCOUNT_ID = re.compile(
    r"^/subscriptions/([a-fA-F0-9-]{36})/resourceGroups/([^/]+)/providers/"
    r"Microsoft\.CognitiveServices/accounts/([^/]+)$", re.IGNORECASE)


class FoundryError(ValueError):
    """Invalid Foundry scope, response, or inventory data."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def account_id(value: str) -> tuple[str, str, str, str]:
    if not isinstance(value, str) or len(value) > 500:
        raise FoundryError("Expected a Cognitive Services ARM account resource ID")
    match = _ACCOUNT_ID.fullmatch(value)
    if not match or not _SUB.fullmatch(match[1]) or not _ID.fullmatch(match[2]) or not _ID.fullmatch(match[3]):
        raise FoundryError("Account must be /subscriptions/<UUID>/resourceGroups/<RG>/providers/Microsoft.CognitiveServices/accounts/<NAME>")
    subscription, group, name = match.groups()
    canonical = (f"/subscriptions/{subscription.lower()}/resourceGroups/{group}/providers/"
                 f"Microsoft.CognitiveServices/accounts/{name}")
    return canonical, subscription.lower(), group, name


def project_endpoint(value: str) -> str:
    if not isinstance(value, str) or len(value) > 350:
        raise FoundryError("Invalid Foundry project endpoint")
    url = urlsplit(value)
    host = url.hostname or ""
    if (url.scheme != "https" or not host.endswith(".services.ai.azure.com") or
            len(host.split(".")) != 5 or not _ID.fullmatch(host.split(".")[0]) or
            url.username or url.password or url.port or url.query or url.fragment or
            not re.fullmatch(r"/api/projects/[A-Za-z0-9_.-]{1,90}/?", url.path)):
        raise FoundryError("Project endpoint must be https://<resource>.services.ai.azure.com/api/projects/<project>")
    return f"https://{host}{url.path.rstrip('/')}"


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict) or type(config.get("enabled", False)) is not bool:
        raise FoundryError("foundry.enabled must be a YAML boolean")
    for field, limit in (("subscriptions", 30), ("accounts", 100), ("projects", 100)):
        values = config.get(field, [])
        if not isinstance(values, list) or len(values) > limit or not all(isinstance(v, str) for v in values):
            raise FoundryError(f"foundry.{field} must be a bounded list of strings")
        if len(set(v.lower() for v in values)) != len(values):
            raise FoundryError(f"foundry.{field} includes duplicates")
    for subscription in config.get("subscriptions", []):
        if not _SUB.fullmatch(subscription):
            raise FoundryError("Invalid Azure subscription UUID")
    for resource in config.get("accounts", []):
        account_id(resource)
    for endpoint in config.get("projects", []):
        project_endpoint(endpoint)
    if set(s.lower() for s in config.get("subscriptions", [])) & {account_id(a)[1] for a in config.get("accounts", [])}:
        raise FoundryError("Do not configure explicit accounts and subscription discovery for the same subscription")
    for key, low, high in (("max_pages", 1, 100), ("max_accounts", 1, 500), ("max_deployments", 1, 5000),
                           ("max_response_bytes", 10000, 10000000), ("timeout_seconds", 1, 120)):
        value = config.get(key, {"max_pages": 25, "max_accounts": 100, "max_deployments": 1500,
                                 "max_response_bytes": 3000000, "timeout_seconds": 20}[key])
        if type(value) is not int or not low <= value <= high:
            raise FoundryError(f"foundry.{key} must be an integer between {low} and {high}")
    return config


def empty_state() -> dict:
    return {"schema_version": SCHEMA_VERSION, "state_version": 0,
            "created_at": utc_now(), "updated_at": utc_now(),
            "deployments": {}, "source_checks": {}, "events": []}


def load_state(path: Path) -> dict:
    if not Path(path).exists():
        return empty_state()
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FoundryError("Cannot read Foundry state file") from exc
    if (not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION or
            type(state.get("state_version")) is not int or
            not isinstance(state.get("deployments"), dict) or
            not isinstance(state.get("source_checks"), dict) or
            not isinstance(state.get("events"), list)):
        raise FoundryError("Invalid Foundry state; restore a known-good backup")
    return state


def save_state(path: Path, state: dict) -> None:
    _atomic_json(Path(path), state)


def _credential(credential):
    if credential is not None:
        return credential
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise FoundryError("Install azure-identity to authenticate to Microsoft Foundry") from exc
    # No interactive browser prompts during scheduled inventory jobs.
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def _get_token(credential, scope: str) -> str:
    try:
        token = credential.get_token(scope).token
    except Exception as exc:
        # Token acquisition errors can contain tenant/resource details.
        raise FoundryError("Azure Entra token acquisition failed; check az login/identity and RBAC") from None
    if not isinstance(token, str) or not token:
        raise FoundryError("Azure Entra returned no access token")
    return token


def _url_for(base: str, *, api_version: str) -> str:
    return f"{base}?api-version={api_version}"


def _validate_page_url(url: str, root: str, api_version: str) -> str:
    """Never send an ARM/Foundry bearer token to a response-supplied host/path."""
    if not isinstance(url, str) or len(url) > 4000:
        raise FoundryError("Invalid pagination URL")
    target, expected = urlsplit(url), urlsplit(root)
    if (target.scheme != "https" or target.hostname != expected.hostname or
            target.port or target.username or target.password or target.fragment or
            target.path != expected.path or target.path.endswith("/../") or
            any(c in url for c in "\r\n\t")):
        raise FoundryError("Unsafe pagination URL: host or path changed")
    if parse_qs(target.query).get("api-version") != [api_version]:
        raise FoundryError("Unsafe pagination URL: API version changed")
    return url


def _json_get(client, url: str, token: str, timeout: int, max_bytes: int) -> dict:
    # Request URL is already fixed or validated against a fixed operation root.
    try:
        with client.stream("GET", url, headers={"Authorization": f"Bearer {token}",
                "Accept": "application/json"}, timeout=timeout, follow_redirects=False) as response:
            if response.status_code != 200:
                raise FoundryError(f"Microsoft Foundry returned HTTP {response.status_code}")
            content_type = response.headers.get("content-type", "")
            if content_type and "json" not in content_type.lower():
                raise FoundryError("Microsoft Foundry returned unexpected content type")
            chunks, total = [], 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise FoundryError("Foundry response exceeds configured maximum size")
                chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
    except (httpx.HTTPError, UnicodeError, ValueError) as exc:
        if isinstance(exc, FoundryError):
            raise
        raise FoundryError(f"Foundry request or JSON failed ({type(exc).__name__})") from None
    if not isinstance(payload, dict):
        raise FoundryError("Foundry response must be a JSON object")
    return payload


def _pages(client, base: str, token: str, *, api_version: str, max_pages: int, timeout: int,
           max_bytes: int, max_items: int) -> list[dict]:
    values, seen = [], set()
    url = _url_for(base, api_version=api_version)
    for _ in range(max_pages):
        _validate_page_url(url, base, api_version)
        if url in seen:
            raise FoundryError("Foundry pagination repeated a continuation URL")
        seen.add(url)
        payload = _json_get(client, url, token, timeout, max_bytes)
        items = payload.get("value")
        if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
            raise FoundryError("Foundry response omitted a valid value array")
        if len(values) + len(items) > max_items:
            raise FoundryError("Foundry inventory exceeded configured item limit")
        values.extend(items)
        next_url = payload.get("nextLink")
        if next_url in (None, ""):
            return values
        url = _validate_page_url(next_url, base, api_version)
    raise FoundryError("Foundry pagination exceeded configured page limit")


def _text(value, name: str, *, required=False, limit=250) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise FoundryError(f"Invalid {name} in Foundry response")
    return value.strip()


def _deployment_arm(raw: dict, resource: str, region: str | None, source_id: str) -> dict:
    acct, subscription, group, name = account_id(resource)
    deployment_name = _text(raw.get("name"), "ARM deployment name", required=True, limit=128)
    expected = f"{acct}/deployments/{deployment_name}"
    actual = raw.get("id")
    if not isinstance(actual, str) or actual.lower() != expected.lower():
        raise FoundryError("ARM response deployment ID does not belong to the requested account")
    props = raw.get("properties")
    if not isinstance(props, dict) or not isinstance(props.get("model"), dict):
        raise FoundryError("ARM deployment has no model metadata")
    model = props["model"]
    model_name = _text(model.get("name"), "ARM model name", required=True)
    fmt = _text(model.get("format"), "ARM model format")
    publisher = _text(model.get("publisher"), "ARM publisher") or fmt
    version = _text(model.get("version"), "ARM version")
    sku = raw.get("sku") or {}
    if not isinstance(sku, dict):
        raise FoundryError("Invalid ARM deployment SKU")
    fields = {
        "deployment_id": f"arm:{actual.lower()}", "source_id": source_id,
        "source_kind": "arm_account", "resource_id": actual,
        "subscription_id": subscription, "resource_group": group, "account_name": name,
        "project_endpoint": None, "deployment_name": deployment_name,
        "publisher": publisher, "publisher_basis": "publisher" if model.get("publisher") else "model_format_proxy",
        "model_name": model_name, "model_version": version, "model_format": fmt,
        "sku_name": _text(sku.get("name"), "SKU"),
        "deployment_type": _text(sku.get("name"), "deployment type"),
        "resource_region": region,
        "version_upgrade_option": _text(props.get("versionUpgradeOption"), "upgrade option"),
        "rai_policy_name": _text(props.get("raiPolicyName"), "RAI policy"),
        "provisioning_state": _text(props.get("provisioningState"), "provisioning state"),
        "source_url": _url_for(f"https://{ARM_HOST}{acct}/deployments", api_version=ARM_API_VERSION),
    }
    fields["evidence_sha256"] = digest(fields)
    return fields


def _deployment_project(raw: dict, endpoint: str, source_id: str) -> dict:
    deployment_name = _text(raw.get("name"), "project deployment name", required=True, limit=128)
    model_name = _text(raw.get("modelName"), "project model name", required=True)
    publisher = _text(raw.get("modelPublisher"), "project model publisher", required=True)
    version = _text(raw.get("modelVersion"), "project model version")
    if raw.get("type") not in (None, "ModelDeployment"):
        raise FoundryError("Unexpected non-model project deployment type")
    sku = raw.get("sku") or {}
    if not isinstance(sku, dict):
        raise FoundryError("Invalid project SKU")
    fields = {
        "deployment_id": f"project:{endpoint.lower()}#{deployment_name.lower()}",
        "source_id": source_id, "source_kind": "foundry_project", "resource_id": None,
        "subscription_id": None, "resource_group": None,
        "account_name": urlsplit(endpoint).hostname.split(".")[0],
        "project_endpoint": endpoint, "deployment_name": deployment_name,
        "publisher": publisher, "publisher_basis": "model_publisher",
        "model_name": model_name, "model_version": version, "model_format": None,
        "sku_name": _text(sku.get("name"), "project SKU"),
        "deployment_type": "ModelDeployment", "resource_region": None,
        "version_upgrade_option": None, "rai_policy_name": None,
        "provisioning_state": None,
        "source_url": _url_for(f"{endpoint}/deployments", api_version=PROJECT_API_VERSION),
    }
    fields["evidence_sha256"] = digest(fields)
    return fields


def _account_rows(client, resource: str, token: str, source_id: str, config: dict,
                  region: str | None = None) -> list[dict]:
    base = f"https://{ARM_HOST}{resource}/deployments"
    raw = _pages(client, base, token, api_version=ARM_API_VERSION,
                 max_pages=config.get("max_pages", 25), timeout=config.get("timeout_seconds", 20),
                 max_bytes=config.get("max_response_bytes", 3000000),
                 max_items=config.get("max_deployments", 1500))
    rows = [_deployment_arm(item, resource, region, source_id) for item in raw]
    if len({r["deployment_id"] for r in rows}) != len(rows):
        raise FoundryError("Duplicate ARM deployment identity")
    return rows


def collect(config: dict, *, client=None, credential=None, at: str | None = None) -> list[dict]:
    """Collect independent scopes; failed/partial scopes never erase prior evidence."""
    config = validate_config(config)
    if not config.get("enabled", False):
        raise FoundryError("Enable foundry in config.yaml before refreshing")
    subscriptions = [s.lower() for s in config.get("subscriptions", [])]
    accounts = [account_id(a)[0] for a in config.get("accounts", [])]
    projects = [project_endpoint(p) for p in config.get("projects", [])]
    if not (subscriptions or accounts or projects):
        raise FoundryError("Configure at least one foundry subscription, account or project")
    own_client = client is None
    client = client or httpx.Client(follow_redirects=False)
    credential = _credential(credential)
    at = at or utc_now()
    results = []
    tokens = {}
    def token(scope):
        if scope not in tokens:
            tokens[scope] = _get_token(credential, scope)
        return tokens[scope]
    try:
        for resource in accounts:
            _, sub, _, _ = account_id(resource)
            sid = f"arm:account:{resource.lower()}"
            try:
                rows = _account_rows(client, resource, token(ARM_SCOPE), sid, config)
                results.append({"source_id": sid, "status": "success", "at": at, "deployments": rows})
            except FoundryError as exc:
                results.append({"source_id": sid, "status": "unavailable", "at": at,
                                "message": str(exc), "deployments": []})
        for sub in subscriptions:
            sid = f"arm:subscription:{sub}"
            rows = []; message = None; incomplete = False
            try:
                base = f"https://{ARM_HOST}/subscriptions/{sub}/providers/Microsoft.CognitiveServices/accounts"
                resource_accounts = _pages(client, base, token(ARM_SCOPE), api_version=ARM_API_VERSION,
                    max_pages=config.get("max_pages", 25), timeout=config.get("timeout_seconds", 20),
                    max_bytes=config.get("max_response_bytes", 3000000), max_items=config.get("max_accounts", 100))
                for account in resource_accounts:
                    resource = account.get("id")
                    canonical, actual_sub, _, _ = account_id(resource)
                    if actual_sub != sub:
                        raise FoundryError("Account list returned a different subscription")
                    try:
                        discovered = _account_rows(client, canonical, token(ARM_SCOPE), sid, config,
                            region=_text(account.get("location"), "account region"))
                        rows.extend(discovered)
                        if len(rows) > config.get("max_deployments", 1500):
                            raise FoundryError("Subscription deployments exceed configured limit")
                    except FoundryError as exc:
                        incomplete = True
                        message = "One or more account deployment lists failed; subset retained"
                if len({r["deployment_id"] for r in rows}) != len(rows):
                    raise FoundryError("Duplicate subscription deployment ID")
                results.append({"source_id": sid, "status": "partial" if incomplete else "success",
                                "at": at, "message": message, "deployments": rows})
            except FoundryError as exc:
                results.append({"source_id": sid, "status": "unavailable", "at": at,
                                "message": str(exc), "deployments": []})
        for endpoint in projects:
            sid = f"project:{endpoint.lower()}"
            try:
                base = f"{endpoint}/deployments"
                raw = _pages(client, base, token(PROJECT_SCOPE), api_version=PROJECT_API_VERSION,
                    max_pages=config.get("max_pages", 25), timeout=config.get("timeout_seconds", 20),
                    max_bytes=config.get("max_response_bytes", 3000000), max_items=config.get("max_deployments", 1500))
                rows = [_deployment_project(d, endpoint, sid) for d in raw]
                if len({r["deployment_id"] for r in rows}) != len(rows):
                    raise FoundryError("Duplicate project deployment identity")
                results.append({"source_id": sid, "status": "success", "at": at, "deployments": rows})
            except FoundryError as exc:
                results.append({"source_id": sid, "status": "unavailable", "at": at,
                                "message": str(exc), "deployments": []})
    finally:
        if own_client:
            client.close()
    return results


def merge(state: dict, outcomes: list[dict]) -> tuple[dict, list[dict]]:
    """Append evidence and events. Missing results are never classified as deletions."""
    result = copy.deepcopy(state)
    events = []
    if len(outcomes) > 250:
        raise FoundryError("Too many Foundry source batches")
    seen_sources = set()
    at = utc_now()
    for outcome in outcomes:
        if not isinstance(outcome, dict) or outcome.get("status") not in ("success", "partial", "unavailable"):
            raise FoundryError("Malformed Foundry collection outcome")
        sid = outcome.get("source_id")
        if not isinstance(sid, str) or len(sid) > 550 or sid in seen_sources:
            raise FoundryError("Invalid or duplicate Foundry source scope")
        seen_sources.add(sid)
        checked_at = outcome.get("at") or at
        status = outcome["status"]
        rows = outcome.get("deployments")
        if not isinstance(rows, list) or (status == "unavailable" and rows):
            raise FoundryError("Invalid Foundry deployment batch")
        before = result["source_checks"].get(sid, {})
        result["source_checks"][sid] = {"status": status, "checked_at": checked_at,
            "last_success_at": checked_at if status == "success" else before.get("last_success_at"),
            "count": len(rows) if status != "unavailable" else None,
            "message": outcome.get("message")}
        if status != "success":
            events.append({"at": checked_at, "type": "SOURCE_PARTIAL" if status == "partial" else "SOURCE_UNAVAILABLE",
                           "source_id": sid, "detail": outcome.get("message")})
        current = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("source_id") != sid or not isinstance(row.get("deployment_id"), str):
                raise FoundryError("Deployment scope mismatch")
            key = row["deployment_id"]
            if key in current:
                raise FoundryError("Duplicate deployment within a Foundry scope")
            current.add(key)
            value = {k: v for k, v in row.items() if k != "evidence_sha256"}
            if row.get("evidence_sha256") != digest(value):
                raise FoundryError("Deployment evidence SHA mismatch")
            prior = result["deployments"].get(key)
            if prior and prior["source_id"] != sid:
                # Explicit account and subscription scans can overlap. Preserve
                # account identity, do not invent independent duplicate deployments.
                if prior["source_kind"] == "arm_account" and row["source_kind"] == "arm_account" and \
                        prior.get("resource_id", "").lower() == row.get("resource_id", "").lower():
                    pass
                else:
                    raise FoundryError("Foundry deployment identity reused across incompatible sources")
            if prior and prior["evidence_sha256"] == row["evidence_sha256"]:
                prior["last_observed_at"] = checked_at
                if prior.get("observation_status") != "observed":
                    prior["observation_status"] = "observed"
                    events.append({"at": checked_at, "type": "DEPLOYMENT_REOBSERVED", "deployment_id": key})
                continue
            history = list(prior.get("history", [])) if prior else []
            if prior:
                history.append({"evidence_sha256": prior["evidence_sha256"],
                    "observed_at": prior["last_observed_at"],
                    "model_name": prior.get("model_name"), "model_version": prior.get("model_version"),
                    "publisher": prior.get("publisher"), "version_upgrade_option": prior.get("version_upgrade_option")})
            result["deployments"][key] = {**row, "first_seen_at": prior["first_seen_at"] if prior else checked_at,
                "last_observed_at": checked_at, "observation_status": "observed", "history": history[-_MAX_HISTORY:]}
            events.append({"at": checked_at, "type": "DEPLOYMENT_CHANGED" if prior else "NEW_DEPLOYMENT",
                           "deployment_id": key, "source_id": sid,
                           "previous_sha256": prior.get("evidence_sha256") if prior else None,
                           "evidence_sha256": row["evidence_sha256"]})
        if status == "success":
            for key, previous in result["deployments"].items():
                if previous["source_id"] == sid and key not in current and previous.get("observation_status") == "observed":
                    previous["observation_status"] = "not_observed_in_latest_complete_scan"
                    events.append({"at": checked_at, "type": "NOT_OBSERVED_ON_RESCAN", "source_id": sid,
                                   "deployment_id": key, "detail": "Requires verification; not a deletion finding"})
    if len(result["deployments"]) > 10000:
        raise FoundryError("Foundry state exceeded maximum deployment count")
    if events:
        result["state_version"] += 1
        result["updated_at"] = at
        result["events"] = (result["events"] + events)[-_MAX_EVENTS:]
    return result, events


def summary(state: dict) -> dict:
    status_counts = Counter(item.get("observation_status", "unknown") for item in state["deployments"].values())
    sources = Counter(item.get("status", "unknown") for item in state["source_checks"].values())
    return {"state_version": state["state_version"],
            "deployments": len(state["deployments"]), "observed": status_counts.get("observed", 0),
            "not_observed_in_latest_complete_scan": status_counts.get("not_observed_in_latest_complete_scan", 0),
            "source_checks": len(state["source_checks"]), "unavailable_sources": sources.get("unavailable", 0),
            "partial_sources": sources.get("partial", 0), "events": len(state["events"])}


def export_json(state: dict, out_dir: Path) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(out_dir / "foundry_deployments.json", {"schema_version": 1, "deployments": state["deployments"]})
    _atomic_json(out_dir / "foundry_sources.json", {"schema_version": 1, "source_checks": state["source_checks"]})
    _atomic_json(out_dir / "foundry_events.json", {"schema_version": 1, "events": state["events"]})
    _atomic_json(out_dir / "foundry_summary.json", summary(state))


def screening(state: dict, register: dict, identity_state: dict, identity_nodes: dict) -> list[dict]:
    """Read-only triage. Never infer permission or apply a frontier-policy decision."""
    from .identity import link_status, suggestions
    register_records = {"register:" + r["record_id"]: r for r in register.get("records", [])}
    suggested = set()
    for hint in suggestions(identity_nodes, identity_state):
        suggested.add(hint["left_id"])
        suggested.add(hint["right_id"])
    results = []
    for deployment_id, item in sorted(state.get("deployments", {}).items()):
        node_id = "foundry:" + deployment_id
        related = []
        for link in identity_state.get("links", {}).values():
            if node_id not in (link.get("left_id"), link.get("right_id")):
                continue
            related.append(link)
        linked_register = []
        stale = False
        for link in related:
            if link_status(link, identity_nodes) != "confirmed":
                if link.get("status") == "confirmed":
                    stale = True
                continue
            other = link["right_id"] if link["left_id"] == node_id else link["left_id"]
            if other in register_records:
                linked_register.append((other, register_records[other]))
        classifications = sorted({r["organization"].get("Organization Classification", "Unknown")
                                  for _, r in linked_register})
        if item.get("observation_status") != "observed":
            status = "Not observed on complete rescan; investigate"
        elif stale:
            status = "Reviewed identity stale; reassess"
        elif "Restricted" in classifications:
            status = "Potential restricted-model exposure; investigate"
        elif linked_register:
            status = "Linked register record; deployment permission not established"
        elif related:
            status = "Identity link exists without confirmed register correspondence"
        elif node_id in suggested:
            status = "Unconfirmed identity suggestion; review"
        else:
            status = "Identity unresolved; assess"
        results.append({"deployment_id": deployment_id, "model_name": item["model_name"],
                        "model_version": item.get("model_version"),
                        "register_ids": [record_id for record_id, _ in linked_register],
                        "register_classifications": classifications,
                        "screening_status": status,
                        "note": "Triage only: hosted identity does not establish checkpoint equivalence, "
                                "application approval, or policy compliance"})
    return results


def add_excel_sheets(workbook_path: Path, state: dict, screening_rows: list[dict] | None = None) -> None:
    """Append Foundry-only worksheets, preserving existing workbook and text safety."""
    import tempfile
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    workbook_path = Path(workbook_path)
    wb = load_workbook(workbook_path)
    specs = [
      ("Foundry Screening",
       ["Deployment ID", "Model Name", "Model Version", "Linked Register IDs", "Register Classifications", "Screening Status", "Limitation"],
       [[row["deployment_id"], row["model_name"], row.get("model_version"),
         ", ".join(row["register_ids"]), ", ".join(row["register_classifications"]),
         row["screening_status"], row["note"]]
        for row in (screening_rows or [])]),
      ("Foundry Deployments",
       ["Deployment ID", "Source Kind", "Account / Project", "Deployment", "Model Publisher",
        "Model Name", "Model Version", "Publisher Basis", "Deployment SKU", "Resource Region",
        "Auto Upgrade Option", "RAI Policy Name", "Provisioning State", "Observation Status",
        "First Seen UTC", "Last Seen UTC", "Evidence SHA256", "Source URL"],
       [[key, item["source_kind"], item.get("account_name") or item.get("project_endpoint"),
         item["deployment_name"], item.get("publisher"), item["model_name"], item.get("model_version"),
         item.get("publisher_basis"), item.get("sku_name"), item.get("resource_region"),
         item.get("version_upgrade_option"), item.get("rai_policy_name"), item.get("provisioning_state"),
         item.get("observation_status"), item.get("first_seen_at"), item.get("last_observed_at"),
         item.get("evidence_sha256"), item.get("source_url")]
        for key, item in sorted(state["deployments"].items())]),
      ("Deployment Intelligence Events",
       ["Timestamp UTC", "Event Type", "Deployment ID", "Source", "Previous SHA256", "Evidence SHA256", "Description"],
       [[event.get("at"), event["type"], event.get("deployment_id"), event.get("source_id"),
         event.get("previous_sha256"), event.get("evidence_sha256"), event.get("detail")]
        for event in state["events"]]),
      ("Foundry Sources", ["Source Scope", "Status", "Last Attempt UTC", "Last Success UTC", "Count", "Note"],
       [[key, source["status"], source["checked_at"], source.get("last_success_at"),
         source.get("count"), source.get("message")]
        for key, source in sorted(state["source_checks"].items())]),
    ]
    for name, headers, rows in specs:
        if name in wb.sheetnames:
            del wb[name]
        ws = wb.create_sheet(name)
        ws.sheet_view.showGridLines = False
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="173952")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[1].height = 32
        for values in rows:
            ws.append(values)
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        for index in range(1, len(headers) + 1):
            ws.column_dimensions[get_column_letter(index)].width = 52 if index in (1, 3, 6, 17, 18) else 24
        ws.freeze_panes = "E2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(ws.max_row, 2)}"
    fd, temporary = tempfile.mkstemp(prefix=".foundry-export-", suffix=".xlsx", dir=workbook_path.parent)
    os.close(fd)
    try:
        wb.save(temporary)
        os.replace(temporary, workbook_path)
    finally:
        wb.close()
        if os.path.exists(temporary):
            os.unlink(temporary)
