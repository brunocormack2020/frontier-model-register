"""Read-only, curated developer and Hugging Face discovery.

This module does not infer frontier classification, execute code, download weights,
or update approved organization model records. Failures retain previous evidence.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote

import httpx

SCHEMA_VERSION = 1
_MISTRAL_URL = "https://api.mistral.ai/v1/models"
_XAI_URL = "https://api.x.ai/v1/language-models"
_HF_API = "https://huggingface.co/api/models"
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40,64}\Z")
_MAX_EVENTS = 2000
_MAX_HISTORY = 12


class IntelligenceError(ValueError):
    """A bounded, actionable intelligence collection/state error."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".intelligence-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def empty_state():
    return {"schema_version": SCHEMA_VERSION, "state_version": 0, "created_at": _now(),
            "updated_at": _now(), "candidates": {}, "source_checks": {}, "events": []}


def load_state(path: Path):
    if not path.exists():
        return empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IntelligenceError("Cannot read intelligence state JSON") from exc
    if (not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION or
            not isinstance(state.get("candidates"), dict) or
            not isinstance(state.get("source_checks"), dict) or
            not isinstance(state.get("events"), list)):
        raise IntelligenceError("Unsupported or invalid intelligence state; restore a known-good version")
    return state


def save_state(path: Path, state: dict):
    _atomic_json(path, state)


def _name(value, label):
    if not isinstance(value, str) or not _ID_RE.fullmatch(value) or value in (".", ".."):
        raise IntelligenceError(f"Invalid {label}; use a publisher or repository slug")
    return value


def _repo(value):
    if not isinstance(value, str) or len(value) > 260 or value.count("/") != 1:
        raise IntelligenceError("A Hugging Face repository must be publisher/repository")
    author, name = value.split("/", 1)
    return f"{_name(author, 'publisher')}/{_name(name, 'repository')}"


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise IntelligenceError("intelligence config must be a mapping")
    if not isinstance(config.get("enabled", False), bool):
        raise IntelligenceError("intelligence.enabled must be a YAML boolean")
    apis = config.get("apis", {})
    if not isinstance(apis, dict):
        raise IntelligenceError("intelligence.apis must be a mapping")
    for key in ("mistral", "xai"):
        item = apis.get(key, {})
        if not isinstance(item, dict) or not isinstance(item.get("enabled", False), bool):
            raise IntelligenceError(f"intelligence.apis.{key}.enabled must be a boolean")
    hf = config.get("huggingface", {})
    if not isinstance(hf, dict) or not isinstance(hf.get("enabled", False), bool):
        raise IntelligenceError("intelligence.huggingface.enabled must be a boolean")
    repos, publishers = hf.get("repositories", []), hf.get("publishers", [])
    if not isinstance(repos, list) or len(repos) > 100 or not isinstance(publishers, list) or len(publishers) > 20:
        raise IntelligenceError("Hugging Face watchlists must be bounded lists")
    for repo_id in repos:
        _repo(repo_id)
    for publisher in publishers:
        if not isinstance(publisher, dict):
            raise IntelligenceError("Hugging Face publisher selection must be a mapping")
        _name(publisher.get("name"), "publisher")
        prefixes = publisher.get("prefixes", [])
        if (not isinstance(prefixes, list) or not prefixes or len(prefixes) > 20 or
                any(not isinstance(p, str) or not p.strip() or len(p) > 64 for p in prefixes)):
            raise IntelligenceError("Publisher prefixes must be a nonempty bounded list")
        limit = publisher.get("max_results", 15)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise IntelligenceError("Publisher max_results must be 1–100")
    seconds = config.get("timeout_seconds", 20)
    if type(seconds) not in (int, float) or not 1 <= seconds <= 120:
        raise IntelligenceError("intelligence.timeout_seconds must be 1–120")
    max_bytes = config.get("max_response_bytes", 3_000_000)
    if type(max_bytes) is not int or not 10_000 <= max_bytes <= 10_000_000:
        raise IntelligenceError("intelligence.max_response_bytes must be 10000–10000000")
    return config


def _get_json(client, url, headers: dict, *, params=None, timeout=20, max_bytes=3_000_000):
    """Only callers supply fixed HTTPS endpoints, never URLs from API responses."""
    if not url.startswith((_MISTRAL_URL, _XAI_URL, _HF_API)):
        raise IntelligenceError("Unexpected external endpoint")
    try:
        with client.stream("GET", url, headers=headers, params=params, timeout=timeout,
                           follow_redirects=False) as response:
            if response.status_code != 200:
                raise IntelligenceError(f"HTTP {response.status_code}")
            kind = response.headers.get("content-type", "")
            if kind and not ("json" in kind.lower()):
                raise IntelligenceError("Unexpected non-JSON source response")
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise IntelligenceError("Response exceeds configured size bound")
                chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
    except (httpx.HTTPError, ValueError, UnicodeError) as exc:
        if isinstance(exc, IntelligenceError):
            raise
        # Neither response bodies nor request URLs/headers are exposed in errors.
        raise IntelligenceError(f"Request or JSON failed ({type(exc).__name__})") from exc
    return payload


def _api_candidates(provider, payload, at):
    key = "data" if provider == "Mistral" else "models"
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise IntelligenceError(f"{provider} response missing expected {key} array")
    result = []
    for item in payload[key]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise IntelligenceError(f"{provider} response includes invalid model")
        model_id = item["id"].strip()
        if not model_id or len(model_id) > 180:
            raise IntelligenceError(f"{provider} response includes invalid model ID")
        if provider == "Mistral":
            model_type = str(item.get("type", item.get("TYPE", ""))).lower()
            if model_id.startswith(("ft:", "ft-")) or model_type in ("fine-tuned", "fine_tuned") or item.get("job"):
                continue  # Customer-owned fine tunes are not public publisher releases.
            details = {k: item[k] for k in ("created", "root", "max_context_length", "capabilities", "archived", "owned_by") if k in item}
            url = _MISTRAL_URL
        else:
            # /language-models is restricted to chat and image-understanding models.
            details = {k: item[k] for k in ("created", "version", "fingerprint", "context_length", "input_modalities", "output_modalities", "owned_by") if k in item}
            url = _XAI_URL
        aliases = item.get("aliases") or []
        if not isinstance(aliases, list) or len(aliases) > 60:
            raise IntelligenceError("Malformed or excessive API alias list")
        aliases = [a for a in aliases if isinstance(a, str) and len(a) <= 180]
        result.append(_candidate(
            key=f"api:{provider}:{model_id}", source_type="developer_api", publisher=provider,
            name=model_id, url=url, details={"api_model_id": model_id, "aliases": sorted(set(aliases)), "catalogue_provider": provider, "developer_identity_confirmed": False, **details},
            retrieved_at=at))
    if len(result) > 1500:
        raise IntelligenceError("API model inventory unexpectedly large; cannot safely import")
    return result


def _candidate(*, key, source_type, publisher, name, url, details, retrieved_at):
    try:
        digest = _hash({"source_type":source_type, "publisher":publisher, "model":name, "details":details})
    except (TypeError, ValueError) as exc:
        raise IntelligenceError("Unsupported model metadata type") from exc
    return {"candidate_id":key, "source_type":source_type, "publisher":publisher,
            "model":name, "source_url":url, "details":details,
            "content_sha256":digest, "retrieved_at":retrieved_at}


def _hf_candidate(payload, expected_repo, at):
    if not isinstance(payload, dict) or payload.get("id") != expected_repo:
        raise IntelligenceError("Hugging Face response repository ID mismatch")
    sha = payload.get("sha")
    if sha is not None and (not isinstance(sha, str) or not _SHA_RE.fullmatch(sha.lower())):
        raise IntelligenceError("Hugging Face repository has invalid revision hash")
    card = payload.get("cardData") or {}
    if not isinstance(card, dict):
        card = {}  # Claims may be absent or malformed; don't infer license/lineage.
    file_types = Counter()
    files = payload.get("siblings") or []
    if not isinstance(files, list):
        raise IntelligenceError("Invalid Hugging Face sibling metadata")
    for item in files[:1000]:
        if not isinstance(item, dict):
            continue
        filename = item.get("rfilename", "")
        if not isinstance(filename, str):
            continue
        ext = filename.rsplit("/",1)[-1].rsplit(".",1)[-1].lower() if "." in filename.rsplit("/",1)[-1] else "no_extension"
        if ext:
            file_types[ext[:24]] += 1
    details = {"repository_id":expected_repo, "revision_sha":sha,
               "last_modified":payload.get("lastModified"), "gated":payload.get("gated"),
               "private":payload.get("private"), "pipeline_tag":payload.get("pipeline_tag"),
               "declared_license":card.get("license"), "declared_base_model":card.get("base_model"),
               "file_count_reported":len(files) if files else None,
               "file_extensions":dict(sorted(file_types.items()))}
    # Public Hub popularity metrics (likes/downloads) are excluded to prevent false change events.
    return _candidate(key=f"hf:{expected_repo}", source_type="huggingface_repository",
                      publisher=expected_repo.split("/",1)[0], name=expected_repo,
                      url=f"https://huggingface.co/{expected_repo}", details=details,
                      retrieved_at=at)


def _hf_repo(client, repo_id, headers, *, timeout, max_bytes, at):
    repo_id = _repo(repo_id)
    url = f"{_HF_API}/{quote(repo_id, safe='/')}"
    payload = _get_json(client, url, headers,
                        params={"blobs":"false", "cardData":"true"},
                        timeout=timeout, max_bytes=max_bytes)
    return _hf_candidate(payload, repo_id, at)


def _hf_publisher(client, spec, headers, *, timeout, max_bytes, at):
    publisher = _name(spec["name"], "publisher")
    limit = spec.get("max_results", 15)
    payload = _get_json(client, _HF_API, headers,
                        params={"author":publisher, "limit":limit, "sort":"lastModified",
                                "direction": "-1", "full":"true", "cardData":"true"},
                        timeout=timeout, max_bytes=max_bytes)
    if not isinstance(payload, list):
        raise IntelligenceError("Hugging Face publisher list was not an array")
    if len(payload) > limit:
        raise IntelligenceError("Hugging Face publisher returned beyond requested limit")
    candidates = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise IntelligenceError("Hugging Face publisher contains malformed entry")
        repo_id = _repo(item["id"])
        if repo_id.split("/",1)[0] != publisher:
            raise IntelligenceError("Hugging Face publisher results crossed the author boundary")
        name = repo_id.split("/",1)[1]
        if any(name.lower().startswith(p.lower()) for p in spec["prefixes"]):
            candidates.append(_hf_candidate(item, repo_id, at))
    # Exactly max_results means the query may omit additional matching repos. No deletion inference.
    return candidates, len(payload) == limit


def _specs(config):
    config = validate_config(config)
    if not config.get("enabled", False):
        return []
    specs = []
    for name in ("mistral", "xai"):
        if config.get("apis",{}).get(name,{}).get("enabled",False):
            specs.append((f"api:{name}", name, None))
    hf = config.get("huggingface",{})
    if hf.get("enabled",False):
        for spec in hf.get("publishers",[]):
            specs.append((f"hf:publisher:{spec['name']}", "hf_publisher", spec))
        for repo_id in hf.get("repositories",[]):
            specs.append((f"hf:repo:{repo_id}", "hf_repo", repo_id))
    keys = [s[0] for s in specs]
    if len(set(keys)) != len(keys):
        raise IntelligenceError("Duplicate configured intelligence source")
    return specs


def collect(config: dict, *, client=None, environ=None, at=None):
    """Collect independent source batches; exceptions never erase prior records.

    The caller decides whether to merge and persist successful batches. A Hugging
    Face publisher capped at max_results is marked partial, never exhaustive.
    """
    specs = _specs(config)
    env = os.environ if environ is None else environ
    at = at or _now()
    timeout = config.get("timeout_seconds", 20)
    max_bytes = config.get("max_response_bytes", 3_000_000)
    own_client = client is None
    client = client or httpx.Client(follow_redirects=False)
    outcomes = []
    try:
        for source_id, source_type, selector in specs:
            try:
                if source_type in ("mistral", "xai"):
                    env_key = "MISTRAL_API_KEY" if source_type == "mistral" else "XAI_API_KEY"
                    key = env.get(env_key)
                    if not key:
                        raise IntelligenceError(f"Credential missing: {env_key}")
                    url = _MISTRAL_URL if source_type == "mistral" else _XAI_URL
                    payload = _get_json(client, url, {"Accept":"application/json", "Authorization":f"Bearer {key}"},
                                        timeout=timeout, max_bytes=max_bytes)
                    candidates = _api_candidates("Mistral" if source_type == "mistral" else "xAI", payload, at)
                    partial = False
                else:
                    hf_token = env.get("HF_TOKEN")
                    headers = {"Accept":"application/json", "User-Agent":"FrontierModelRegister/1.2"}
                    if hf_token:
                        headers["Authorization"] = f"Bearer {hf_token}"
                    if source_type == "hf_repo":
                        candidates = [_hf_repo(client, selector, headers, timeout=timeout, max_bytes=max_bytes, at=at)]
                        partial = False
                    else:
                        candidates, partial = _hf_publisher(client, selector, headers,
                                                             timeout=timeout, max_bytes=max_bytes, at=at)
                outcomes.append({"source_id":source_id, "status":"partial" if partial else "success",
                                 "at":at, "candidates":candidates,
                                 "message":"Bounded publisher list; other repositories may be unobserved" if partial else None})
            except IntelligenceError as exc:
                # Error text is limited to safe descriptions constructed in this module.
                outcomes.append({"source_id":source_id, "status":"unavailable", "at":at,
                                 "candidates":[], "message":str(exc)[:160]})
    finally:
        if own_client:
            client.close()
    return outcomes


def _source_keys(outcomes):
    return sorted(x["source_id"] for x in outcomes)


def merge(state: dict, outcomes: list[dict]):
    """Merge only successful observations and retain disappeared/unavailable items.

    A later curated exact repo detail overrides a same-run publisher-list entry.
    Every actual material metadata change reopens human review.
    """
    previous = state
    result = json.loads(json.dumps(previous))
    catalog = result["candidates"]
    recent_events = []
    staged = {}
    for outcome in outcomes:
        source_id, status = outcome["source_id"], outcome["status"]
        if status not in ("success", "partial", "unavailable"):
            raise IntelligenceError("Unexpected collection outcome status")
        if status != "unavailable":
            for candidate in outcome["candidates"]:
                if candidate["candidate_id"] in staged and not source_id.startswith("hf:repo:"):
                    # The explicit repo detail is preferred to the publisher list.
                    continue
                staged[candidate["candidate_id"]] = candidate
        old_source = result["source_checks"].get(source_id, {})
        result["source_checks"][source_id] = {"source_id":source_id, "status":status,
                                               "checked_at":outcome["at"],
                                               "last_success_at":old_source.get("last_success_at") if status == "unavailable" else outcome["at"],
                                               "count":len(outcome["candidates"]) if status != "unavailable" else old_source.get("count",0),
                                               "message":outcome.get("message")}
        if status == "unavailable" and old_source.get("status") != "unavailable":
            recent_events.append({"type":"SOURCE_UNAVAILABLE", "source_id":source_id,
                                  "at":outcome["at"], "detail":"Latest source check failed; prior records retained"})
        elif status == "partial" and old_source.get("status") != "partial":
            recent_events.append({"type":"SOURCE_PARTIAL", "source_id":source_id,
                                  "at":outcome["at"], "detail":"Bounded watchlist; not a complete publisher inventory"})
    for key in sorted(staged):
        observation = staged[key]
        old = catalog.get(key)
        if old is None:
            catalog[key] = {**observation, "first_seen_at":observation["retrieved_at"],
                            "review":{"status":"pending", "reviewed_by":None,"reviewed_at":None,"note":None},
                            "history":[]}
            recent_events.append({"type":"NEW_CANDIDATE", "candidate_id":key,
                                  "at":observation["retrieved_at"], "content_sha256":observation["content_sha256"]})
        elif old["content_sha256"] != observation["content_sha256"]:
            history = list(old.get("history",[]))[-(_MAX_HISTORY-1):]
            history.append({"previous_content_sha256":old["content_sha256"],
                            "previous_details":old["details"], "replaced_at":observation["retrieved_at"]})
            catalog[key] = {**observation, "first_seen_at":old["first_seen_at"],
                            "review":{"status":"pending", "reviewed_by":None,"reviewed_at":None,"note":"Observed metadata changed; re-review required"},
                            "history":history}
            recent_events.append({"type":"METADATA_CHANGED", "candidate_id":key,
                                  "at":observation["retrieved_at"], "previous_sha256":old["content_sha256"],
                                  "content_sha256":observation["content_sha256"]})
        else:
            # Preserve approval history; refresh timestamps don't trigger review.
            catalog[key]["retrieved_at"] = observation["retrieved_at"]
    if outcomes:
        result["state_version"] += 1
        result["updated_at"] = max(o["at"] for o in outcomes)
        result["events"] = (result["events"] + recent_events)[-_MAX_EVENTS:]
    return result, recent_events


def queue(state):
    items = [{"candidate_id":key, "content_sha256":c["content_sha256"],
              "source_url":c["source_url"], "model":c["model"], "publisher":c["publisher"],
              "action":"pending", "review_note":""}
             for key,c in sorted(state["candidates"].items()) if c["review"]["status"] == "pending"]
    return {"schema_version":1, "state_sha256":_hash(state), "items":items,
            "instructions":"Review metadata and source. Use action=acknowledge or reject, with a substantive review_note. Neither action adds or approves an organization model."}


def apply_review(state, request, reviewer, *, at=None):
    if (not isinstance(reviewer,str) or not reviewer.strip() or not isinstance(request,dict) or
            request.get("state_sha256") != _hash(state) or not isinstance(request.get("items"), list)):
        raise IntelligenceError("Intelligence review request is stale or reviewer is missing")
    result = json.loads(json.dumps(state))
    seen, changes = set(), []
    for item in request["items"]:
        key = item.get("candidate_id")
        if (key not in result["candidates"] or key in seen or
                result["candidates"][key]["review"]["status"] != "pending" or
                result["candidates"][key]["content_sha256"] != item.get("content_sha256")):
            raise IntelligenceError("Unknown, duplicate or modified evidence reference")
        seen.add(key)
        action = item.get("action", "pending")
        if action == "pending":
            continue
        if action not in ("acknowledge", "reject") or not str(item.get("review_note","")).strip():
            raise IntelligenceError("Review requires acknowledge/reject and a substantive note")
        reviewed = result["candidates"][key]["review"]
        reviewed.update(status="acknowledged" if action == "acknowledge" else "rejected",
                        reviewed_by=reviewer.strip(), reviewed_at=at or _now(), note=item["review_note"].strip())
        changes.append({"type":"CANDIDATE_REVIEWED", "candidate_id":key,
                        "at":reviewed["reviewed_at"], "action":action, "reviewer":reviewer.strip()})
    if not changes:
        raise IntelligenceError("No intelligence review actions supplied")
    result["state_version"] += 1
    result["updated_at"] = at or _now()
    result["events"] = (result["events"] + changes)[-_MAX_EVENTS:]
    return result


def summary(state):
    statuses = Counter(c["review"]["status"] for c in state["candidates"].values())
    return {"state_version":state["state_version"], "candidates":len(state["candidates"]),
            "pending_reviews":statuses.get("pending",0),
            "acknowledged_candidates":statuses.get("acknowledged",0),
            "rejected_candidates":statuses.get("rejected",0),
            "source_issues":sum(c["status"] in ("partial","unavailable") for c in state["source_checks"].values()),
            "source_checks":len(state["source_checks"]), "events":len(state["events"])}


def export_json(state, output_dir:Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(output_dir / "intelligence_candidates.json", {"schema_version":1,"candidates":state["candidates"]})
    _atomic_json(output_dir / "intelligence_events.json", {"schema_version":1,"events":state["events"]})
    _atomic_json(output_dir / "intelligence_review_queue.json", queue(state))
    _atomic_json(output_dir / "intelligence_summary.json", summary(state))


def add_excel_sheets(workbook_path:Path, state:dict):
    """Add clean, editable candidate/event sheets; preserve original register fields."""
    from openpyxl import load_workbook
    from openpyxl.styles import PatternFill, Font, Alignment
    from openpyxl.utils import get_column_letter
    wb = load_workbook(workbook_path)
    specs = [
        ("Vendor Intelligence", ["Source Type","Publisher","Reported Model / Repository","Revision SHA","Declared License",
                                  "Gated","Review Status","First Seen UTC","Last Observed UTC","Evidence SHA256","Source URL","Candidate ID"],
         [[c["source_type"],c["publisher"],c["model"],c["details"].get("revision_sha") or c["details"].get("version"),
           c["details"].get("declared_license"),str(c["details"].get("gated")) if c["details"].get("gated") is not None else None,
           c["review"]["status"],c["first_seen_at"],c["retrieved_at"],c["content_sha256"],c["source_url"],key]
          for key,c in sorted(state["candidates"].items())]),
        ("Intelligence Events", ["Event UTC","Type","Candidate / Source ID","Previous SHA256","Current SHA256","Detail"],
         [[e.get("at"),e["type"],e.get("candidate_id",e.get("source_id")),
           e.get("previous_sha256"),e.get("content_sha256"),e.get("detail")]
          for e in state["events"]]),
        ("Intelligence Sources", ["Source ID","Status","Last Check UTC","Last Success UTC","Results","Note"],
         [[k,c["status"],c["checked_at"],c.get("last_success_at"),c.get("count"),c.get("message")]
          for k,c in sorted(state["source_checks"].items())]),
    ]
    for sheet, headers, rows in specs:
        if sheet in wb.sheetnames:
            del wb[sheet]
        ws = wb.create_sheet(sheet)
        ws.sheet_view.showGridLines = False
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(name="Arial",size=10,bold=True,color="FFFFFF")
            cell.fill = PatternFill("solid",fgColor="173952")
            cell.alignment = Alignment(wrap_text=True,vertical="center")
        ws.row_dimensions[1].height = 32
        for values in rows:
            # Values are data; never execute untrusted Excel formula strings.
            ws.append([json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,(dict,list)) else v for v in values])
            for cell in ws[ws.max_row]:
                if isinstance(cell.value,str):
                    cell.data_type = "s"
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                cell.font = Font(name="Arial",size=10,color="243D50")
        for index in range(1,len(headers)+1):
            ws.column_dimensions[get_column_letter(index)].width = 56 if index in (3,11,12) else 26
        ws.freeze_panes="D2"
        ws.auto_filter.ref=f"A1:{get_column_letter(len(headers))}{max(2,ws.max_row)}"
    fd,tmp = tempfile.mkstemp(prefix=".intelligence-excel-",suffix=".xlsx",dir=workbook_path.parent)
    os.close(fd)
    try:
        wb.save(tmp)
        os.replace(tmp,workbook_path)
    finally:
        wb.close()
        if os.path.exists(tmp):
            os.unlink(tmp)
