"""Evidence-bound, human-reviewed model identity relationships.

Discovery supplies observations, not checkpoint equivalence. This module never
changes a model, benchmark mapping, organization decision, or intelligence claim.
No transitive inference: a confirmed A~B and B~C does not establish A~C.
"""
from __future__ import annotations

import copy
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .intelligence import IntelligenceError, _atomic_json

SCHEMA_VERSION = 1
RELATIONS = ("same_family", "same_model_version", "hosted_offering_of", "exact_checkpoint")
PROOF_KINDS = ("provider_statement", "deployment_configuration", "signed_artifact_manifest", "independent_validation")
MAX_LINKS = 5000
MAX_SUGGESTIONS = 1500


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
                                   separators=(",", ":"),allow_nan=False).encode("utf-8")).hexdigest()


def empty_state() -> dict:
    return {"schema_version":SCHEMA_VERSION,"state_version":0,"created_at":now(),
            "updated_at":now(),"links":{},"audit":[]}


def load_state(path:Path) -> dict:
    if not Path(path).exists():
        return empty_state()
    try:
        result=json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IntelligenceError("Cannot load model identity state") from exc
    if (not isinstance(result,dict) or result.get("schema_version")!=SCHEMA_VERSION or
            not isinstance(result.get("links"),dict) or not isinstance(result.get("audit"),list) or
            type(result.get("state_version")) is not int):
        raise IntelligenceError("Invalid identity state schema; recover the original file")
    return result


def save_state(path:Path,state:dict) -> None:
    _atomic_json(Path(path),state)


def node_inventory(intelligence:dict, register:dict) -> dict:
    """Keep provider catalogue claims separate from registered model evidence."""
    nodes={}
    for key,c in intelligence.get("candidates",{}).items():
        node_id=f"intel:{key}"
        if not isinstance(c,dict) or not isinstance(c.get("content_sha256"),str):
            raise IntelligenceError("Invalid intelligence candidate for identity reconciliation")
        nodes[node_id]={"node_id":node_id,"kind":"intelligence_candidate",
                        "label":c.get("model") or key,"provider":c.get("publisher"),
                        "source_type":c.get("source_type"),"evidence_sha256":c["content_sha256"],
                        "review_status":c.get("review",{}).get("status","pending"),
                        "source_url":c.get("source_url"),"details":c.get("details") or {}}
    for record in register.get("records",[]):
        key=record.get("record_id")
        if not isinstance(key,str) or not key:
            raise IntelligenceError("Registered record missing identity")
        v=record.get("vendor",{})
        fields={"Provider":v.get("Provider"),"Exact Model ID":v.get("Exact Model ID"),
                "Model Family":v.get("Model Family"),"Version":v.get("Version"),
                "deployment_key":record.get("deployment_key")}
        nodes[f"register:{key}"]={"node_id":f"register:{key}","kind":"registered_model",
             "label":str(v.get("Exact Model ID") or key),"provider":v.get("Provider"),
             "source_type":"organization_register","evidence_sha256":digest(fields),
             "review_status":"registered_not_approved_by_reconciliation",
             "source_url":None,"details":fields}
    return nodes


def snapshot(nodes:dict,identity_state:dict) -> str:
    """Retrieval times and unrelated organization/benchmark edits do not invalidate queues."""
    return digest({"nodes":{k:v["evidence_sha256"] for k,v in sorted(nodes.items())},
                   "identity_state_version":identity_state["state_version"],
                   "links":{k:v.get("updated_at") for k,v in sorted(identity_state["links"].items())}})


def _pair(left:str,right:str,nodes:dict) -> tuple[str,str,str]:
    if (not isinstance(left,str) or not isinstance(right,str) or left==right or
            left not in nodes or right not in nodes):
        raise IntelligenceError("Both distinct identity endpoints must exist in the current inventory")
    a,b=sorted([left,right])
    return a,b,digest([a,b])[:24]


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]","",str(text or "").lower())


# Explicit, curated namespace correspondence. It is an input to candidate
# discovery, NOT proof that an account, publisher, API ID, or checkpoint is genuine.
# In particular, a catalogue API may also list models from other developers.
_PUBLISHER_DEVELOPERS = {
    "mistralai": "Mistral", "xai-org": "xAI", "openai": "OpenAI",
    "google": "Google", "meta-llama": "Meta", "deepseek-ai": "DeepSeek",
    "qwen": "Qwen",
}
_KNOWN_DEVELOPERS = frozenset(_PUBLISHER_DEVELOPERS.values()) | frozenset(("Anthropic",))
_MIN_MATCH_CHARS = 5
_ROLE_PAIRS = (("developer_api", "huggingface_repository"),
               ("developer_api", "organization_register"),
               ("organization_register", "huggingface_repository"))


def _developer(node: dict) -> str | None:
    """Use publisher context to *exclude* cross-developer name coincidences."""
    if node.get("source_type") == "huggingface_repository":
        return _PUBLISHER_DEVELOPERS.get(str(node.get("provider", "")).lower())
    if node.get("source_type") in ("developer_api", "organization_register"):
        name = node.get("provider")
        return name if name in _KNOWN_DEVELOPERS else None
    return None


def _matchable_name(name: str) -> bool:
    # Allow named/versioned short models such as grok-2, but not generic short words.
    return len(name) >= 8 or (len(name) >= _MIN_MATCH_CHARS and
                               any(c.isdigit() for c in name) and
                               any(c.isalpha() for c in name))


def _name_forms(node: dict) -> dict:
    """Return distinct observed identifiers; do not remove versions/size/safety suffixes."""
    kind = node.get("source_type")
    details = node.get("details") or {}
    if kind == "developer_api":
        primary = details.get("api_model_id") or node.get("label")
    elif kind == "huggingface_repository":
        full = details.get("repository_id") or node.get("label") or ""
        # A contradictory display name versus repository ID is not reliable
        # evidence. This also guards against malformed imported state.
        if details.get("repository_id") and node.get("label") != details["repository_id"]:
            return {"primary": "", "composite": "", "aliases": ()}
        primary = full.split("/", 1)[1] if isinstance(full, str) and "/" in full else ""
    elif kind == "organization_register":
        primary = details.get("Exact Model ID") or node.get("label")
        # Google's model-resource prefix is a documented identifier wrapper;
        # remove only this exact wrapper, not arbitrary provider/version text.
        if node.get("provider") == "Google" and isinstance(primary, str) and primary.startswith("models/"):
            primary = primary[len("models/"):]
    else:
        return {"primary": "", "composite": "", "aliases": ()}
    primary = _norm(primary)
    if not _matchable_name(primary):
        primary = ""
    composite = ""
    if kind == "organization_register":
        family, version = details.get("Model Family"), details.get("Version")
        if (isinstance(family, str) and family.strip() and
                isinstance(version, str) and version.strip()):
            composite = _norm(family + " " + version)
        if not _matchable_name(composite) or composite == primary:
            composite = ""
    aliases = ()
    if kind == "developer_api":
        raw = details.get("aliases", [])
        if isinstance(raw, list):
            aliases = tuple(sorted({_norm(alias) for alias in raw[:60]
                                    if isinstance(alias, str) and
                                    _matchable_name(_norm(alias)) and
                                    _norm(alias) != primary}))
    return {"primary": primary, "composite": composite, "aliases": aliases}


def _matching_candidates(nodes: dict):
    """Index exact observed IDs by developer and source; no fuzzy/substring joins."""
    groups = defaultdict(lambda: defaultdict(list))
    forms = {}
    for node in nodes.values():
        developer = _developer(node)
        if developer is None:
            continue
        kind = node.get("source_type")
        if kind not in {"developer_api", "huggingface_repository", "organization_register"}:
            continue
        if not isinstance(node.get("node_id"), str):
            continue
        form = _name_forms(node)
        if not form["primary"]:
            continue
        groups[developer][kind].append(node)
        forms[node["node_id"]] = form
    matches = {}
    for developer in sorted(groups):
        for kind_a, kind_b in _ROLE_PAIRS:
            lefts = groups[developer][kind_a]
            rights = groups[developer][kind_b]
            if not lefts or not rights:
                continue
            primary_right = defaultdict(list)
            composite_right = defaultdict(list)
            for right in rights:
                f = forms[right["node_id"]]
                primary_right[f["primary"]].append(right)
                if f["composite"]:
                    composite_right[f["composite"]].append(right)
            for left in lefts:
                f = forms[left["node_id"]]
                options = []
                for right in primary_right.get(f["primary"], ()):
                    options.append((right, "exact_reported_identifier", 3,
                        "same_model_version", "Full normalized reported identifiers match"))
                # One endpoint is an organization register: a human-entered
                # family+version might agree despite a different exact model ID.
                if kind_a == "organization_register" and f["composite"]:
                    for right in primary_right.get(f["composite"], ()):
                        options.append((right, "register_family_version", 2,
                            "same_family", "Register family + version matches the reported model name, but a distinct variant may be involved"))
                if kind_b == "organization_register":
                    for right in composite_right.get(f["primary"], ()):
                        options.append((right, "register_family_version", 2,
                            "same_family", "Register family + version matches the API model ID, but a distinct variant may be involved"))
                if kind_a == "developer_api":
                    for alias in f["aliases"]:
                        for right in primary_right.get(alias, ()):
                            options.append((right, "mutable_api_alias", 1,
                                "hosted_offering_of", "Name matches a mutable API alias, which may move between releases"))
                for right, rule, priority, relation, explanation in options:
                    left_id, right_id, pair_id = _pair(left["node_id"], right["node_id"], nodes)
                    if pair_id in matches and matches[pair_id][0] >= priority:
                        continue
                    # Same developer is an indexing rule, not a provider or
                    # origin attestation. Keep the exact review limitation.
                    reason = (f"{explanation} across {kind_a} and {kind_b}; "
                              f"curated developer namespace: {developer}; "
                              "checkpoint NOT verified; this is not a model identity assertion")
                    matches[pair_id] = (priority, {
                        "pair_id": pair_id, "left_id": left_id, "right_id": right_id,
                        "reason": reason, "match_rule": rule,
                        "match_strength": ("full_name" if priority == 3 else
                                           "family_version" if priority == 2 else "alias"),
                        "developer_context": developer,
                        "proposed_relation": relation,
                        "suggested_relation": f"{relation} (review required)",
                        "evidence_hashes": {
                            left_id: nodes[left_id]["evidence_sha256"],
                            right_id: nodes[right_id]["evidence_sha256"]},
                    })
                    if len(matches) > MAX_SUGGESTIONS:
                        raise IntelligenceError("Excessive identity suggestions; narrow curated discovery")
    return [entry for _, entry in sorted(matches.values(), key=lambda x: x[1]["pair_id"])]


def suggestions(nodes: dict, identity_state: dict) -> list[dict]:
    """Deterministic, review-only multi-source hints; never assert identity or approval."""
    result = _matching_candidates(nodes)
    for item in result:
        saved = identity_state["links"].get(item["pair_id"])
        item["link_status"] = link_status(saved, nodes) if saved else "unreviewed"
    return result


def link_status(link:dict|None,nodes:dict) -> str:
    if link is None:return "unreviewed"
    if link.get("status")=="revoked":return "revoked"
    endpoints=(link.get("left_id"),link.get("right_id"))
    stored=link.get("evidence_hashes",{})
    if any(k not in nodes or nodes[k]["evidence_sha256"]!=stored.get(k) for k in endpoints):
        return "stale_evidence"
    if link.get("status")=="confirmed" and any(nodes[k]["kind"]=="intelligence_candidate" and nodes[k]["review_status"]!="acknowledged" for k in endpoints):
        return "stale_candidate_review"
    return link.get("status","invalid")


def _item(nodes:dict,left_id:str,right_id:str,reason:str="Manual reconciliation request",relation:str="same_model_version") -> dict:
    left,right,pair_id=_pair(left_id,right_id,nodes)
    return {"pair_id":pair_id,"left_id":left,"right_id":right,"reason":reason,
            "evidence_hashes":{left:nodes[left]["evidence_sha256"],right:nodes[right]["evidence_sha256"]},
            "action":"pending","relation":relation,"evidence_url":"",
            "proof_kind":"","proof_reference":"","review_note":""}


def review_queue(nodes:dict,identity_state:dict) -> dict:
    known=identity_state["links"]
    items=[]
    for s in suggestions(nodes,identity_state):
        # Re-review only unresolved, revoked, or stale links. Do not replay confirmed decisions.
        if s["link_status"] in ("confirmed","rejected"):continue
        items.append(_item(nodes,s["left_id"],s["right_id"],s["reason"],s.get("proposed_relation","same_model_version")))
    return {"schema_version":SCHEMA_VERSION,"snapshot_sha256":snapshot(nodes,identity_state),
            "items":items,"instructions":"Set action=confirm/reject/revoke. Confirm requires acknowledged intelligence evidence, a relation, an HTTPS evidence_url and substantive review_note. exact_checkpoint also requires proof_kind and proof_reference. No transitive equivalence or organization approval."}


def manual_template(nodes:dict,identity_state:dict,left:str,right:str) -> dict:
    return {"schema_version":SCHEMA_VERSION,"snapshot_sha256":snapshot(nodes,identity_state),
            "items":[_item(nodes,left,right)],
            "instructions":"Manual identity link; supply explicit evidence before confirming. This is NOT model approval."}


def _reference(url:str) -> str:
    if not isinstance(url,str) or not 12<=len(url)<=2000:
        raise IntelligenceError("Confirmation requires an HTTPS evidence_url")
    parsed=urlsplit(url)
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or any(c in url for c in "\r\n\t"):
        raise IntelligenceError("Evidence URL must be an HTTPS URL without credentials")
    return url


def apply_review(identity_state:dict,nodes:dict,request:dict,reviewer:str,*,at:str|None=None) -> dict:
    """Atomic, reviewer-attributed pair decisions bound to the current observations."""
    if (not isinstance(reviewer,str) or not reviewer.strip() or len(reviewer)>120 or
            not isinstance(request,dict) or request.get("schema_version")!=SCHEMA_VERSION or
            request.get("snapshot_sha256")!=snapshot(nodes,identity_state) or
            not isinstance(request.get("items"),list)):
        raise IntelligenceError("Identity review request is stale, malformed or reviewer is missing")
    if len(request["items"])>MAX_LINKS:
        raise IntelligenceError("Identity review request exceeds configured maximum")
    result=copy.deepcopy(identity_state)
    seen=set();events=[]; timestamp=at or now()
    for row in request["items"]:
        if not isinstance(row,dict):raise IntelligenceError("Identity review row must be an object")
        left,right,pair_id=_pair(row.get("left_id"),row.get("right_id"),nodes)
        if pair_id in seen or row.get("pair_id")!=pair_id or row.get("left_id")!=left or row.get("right_id")!=right:
            raise IntelligenceError("Identity pair is duplicated, reordered or forged")
        seen.add(pair_id)
        correct={left:nodes[left]["evidence_sha256"],right:nodes[right]["evidence_sha256"]}
        if row.get("evidence_hashes")!=correct:
            raise IntelligenceError("Identity source evidence changed; generate a fresh queue")
        action=row.get("action","pending")
        if action=="pending":continue
        if action not in ("confirm","reject","revoke"):
            raise IntelligenceError("Identity action must be confirm, reject or revoke")
        note=row.get("review_note")
        if not isinstance(note,str) or len(note.strip())<12 or len(note)>4000:
            raise IntelligenceError("Identity review requires a substantive note (12–4000 characters)")
        previous=result["links"].get(pair_id)
        if action=="revoke":
            if not previous or previous.get("status")=="revoked":
                raise IntelligenceError("Can revoke only an existing active/rejected link")
            relation=previous["relation"];proof={"evidence_url":previous.get("evidence_url"),
                 "proof_kind":previous.get("proof_kind"),"proof_reference":previous.get("proof_reference")}
            status="revoked"
        elif action=="reject":
            if previous and previous.get("status")=="confirmed":
                raise IntelligenceError("Revoke a confirmed link before replacing it with a rejection")
            relation="not_same"; proof={"evidence_url":None,"proof_kind":None,"proof_reference":None}
            status="rejected"
        else:
            relation=row.get("relation")
            if relation not in RELATIONS:
                raise IntelligenceError("Choose an explicit supported identity relationship")
            _reference(row.get("evidence_url"))
            if previous and previous.get("status")=="confirmed" and link_status(previous,nodes)=="confirmed" and relation!=previous.get("relation"):
                raise IntelligenceError("Revoke an active confirmed link before changing its relationship type")
            for key in (left,right):
                if nodes[key]["kind"]=="intelligence_candidate" and nodes[key]["review_status"]!="acknowledged":
                    raise IntelligenceError("A model identity cannot be confirmed while source candidate evidence is unreviewed or rejected")
            proof_kind=row.get("proof_kind") or None
            proof_reference=row.get("proof_reference") or None
            if relation=="exact_checkpoint":
                if proof_kind not in PROOF_KINDS or not isinstance(proof_reference,str) or len(proof_reference.strip())<16 or len(proof_reference)>4000:
                    raise IntelligenceError("Exact checkpoint assertion requires named proof_kind and substantive proof_reference")
            elif proof_kind and proof_kind not in PROOF_KINDS:
                raise IntelligenceError("Unsupported proof_kind")
            proof={"evidence_url":row["evidence_url"],"proof_kind":proof_kind,"proof_reference":proof_reference}
            status="confirmed"
        history=list(previous.get("history",[])) if previous else []
        if previous:
            history.append({k:v for k,v in previous.items() if k!="history"})
            history=history[-30:]
        result["links"][pair_id]={"pair_id":pair_id,"left_id":left,"right_id":right,"status":status,
            "relation":relation,"reviewer":reviewer.strip(),"reviewed_at":timestamp,"updated_at":timestamp,
            "review_note":note.strip(),"evidence_hashes":correct,**proof,"history":history}
        events.append({"at":timestamp,"action":action,"pair_id":pair_id,"relation":relation,
                       "reviewer":reviewer.strip(),"evidence_hashes":correct})
    if not events:raise IntelligenceError("No identity decisions submitted")
    if len(result["links"])>MAX_LINKS:raise IntelligenceError("Identity link store exceeds allowed size")
    result["state_version"]+=1
    result["updated_at"]=timestamp
    result["audit"]=(result["audit"]+events)[-5000:]
    return result


def summary(identity_state:dict,nodes:dict) -> dict:
    statuses={}
    for link in identity_state["links"].values():
        key=link_status(link,nodes)
        statuses[key]=statuses.get(key,0)+1
    hints = suggestions(nodes, identity_state)
    source_counts = Counter(node.get("source_type") or "unknown" for node in nodes.values())
    rule_counts = Counter(item["match_rule"] for item in hints)
    return {"identity_state_version":identity_state["state_version"],
            "candidate_and_register_nodes":len(nodes),"source_nodes":dict(sorted(source_counts.items())),
            "suggested_pairs":len(hints),"suggestions_by_rule":dict(sorted(rule_counts.items())),
            "no_suggestions_reason": (None if hints else
                "No eligible cross-source identifier/alias match. Hugging Face-only "
                "discovery rarely establishes a pair; enable authorized Mistral/xAI "
                "API discovery or use identity-template for evidence-backed manual links."),
            "confirmed_links":statuses.get("confirmed",0),"rejected_pairs":statuses.get("rejected",0),
            "stale_links":sum(n for k,n in statuses.items() if k.startswith("stale_")),
            "revoked_links":statuses.get("revoked",0)}


def export_json(identity_state:dict,nodes:dict,output_dir:Path) -> None:
    out=Path(output_dir);out.mkdir(parents=True,exist_ok=True)
    s=suggestions(nodes,identity_state)
    public_links=[{k:v for k,v in link.items() if k!="history"} | {"effective_status":link_status(link,nodes)}
                  for _,link in sorted(identity_state["links"].items())]
    _atomic_json(out/"identity_links.json",{"schema_version":1,"links":public_links})
    _atomic_json(out/"identity_suggestions.json",{"schema_version":1,"suggestions":s})
    _atomic_json(out/"identity_review_queue.json",review_queue(nodes,identity_state))
    _atomic_json(out/"identity_summary.json",summary(identity_state,nodes))


def add_excel_sheets(workbook_path:Path,identity_state:dict,nodes:dict) -> None:
    """Append identity-only views to the existing workbook; never write formulas."""
    import os
    import tempfile
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment,Font,PatternFill
    from openpyxl.utils import get_column_letter
    workbook_path=Path(workbook_path)
    wb=load_workbook(workbook_path)
    hints=suggestions(nodes,identity_state)
    links=identity_state["links"]
    sheets=[
        ("Identity Suggestions",["Pair ID","Left Model / Source","Right Model / Source","Match Basis","Status","Left Evidence SHA","Right Evidence SHA", "Match Rule", "Match Strength", "Suggested Relationship", "Developer Context", "Left Node ID", "Right Node ID", "Left Review", "Right Review"],
         [[s["pair_id"],nodes[s["left_id"]]["label"],nodes[s["right_id"]]["label"],s["reason"],s["link_status"],
           s["evidence_hashes"][s["left_id"]],s["evidence_hashes"][s["right_id"]],s["match_rule"],
           s["match_strength"],s["suggested_relation"],s["developer_context"],s["left_id"],s["right_id"],
           nodes[s["left_id"]].get("review_status"),nodes[s["right_id"]].get("review_status")]
          for s in hints] if hints else [["No automatic suggestions",None,None,
              "No eligible cross-source identifier match. Discover matching official developer API and curated publisher metadata, or generate a manual identity-template.",
              "no_match",None,None,None,None,None,None,None,None,None,None]]),
        ("Model Identity Links",["Pair ID","Left Node ID","Right Node ID","Relation","Effective Status","Reviewer","Reviewed UTC","Evidence URL","Proof Kind","Proof Reference","Review Note"],
         [[pair_id,link["left_id"],link["right_id"],link["relation"],link_status(link,nodes),
           link["reviewer"],link["reviewed_at"],link.get("evidence_url"),link.get("proof_kind"),
           link.get("proof_reference"),link.get("review_note")] for pair_id,link in sorted(links.items())]),
    ]
    for name,headers,rows in sheets:
        if name in wb.sheetnames:del wb[name]
        ws=wb.create_sheet(name);ws.sheet_view.showGridLines=False
        ws.append(headers)
        for cell in ws[1]:
            cell.font=Font(name="Arial",bold=True,color="FFFFFF",size=10)
            cell.fill=PatternFill("solid",fgColor="173952")
            cell.alignment=Alignment(wrap_text=True,vertical="center")
        ws.row_dimensions[1].height=34
        for values in rows:
            ws.append(values)
            for cell in ws[ws.max_row]:
                if isinstance(cell.value,str):cell.data_type="s"
                cell.alignment=Alignment(vertical="top",wrap_text=True)
        for index in range(1,len(headers)+1):
            ws.column_dimensions[get_column_letter(index)].width=55 if index in (2,3,4,10,11,12,13) else 27
        ws.freeze_panes="C2";ws.auto_filter.ref=f"A1:{get_column_letter(len(headers))}{max(2,ws.max_row)}"
    fd,temp=tempfile.mkstemp(prefix=".identity-excel-",suffix=".xlsx",dir=workbook_path.parent)
    os.close(fd)
    try:
        wb.save(temp)
        os.replace(temp,workbook_path)
    finally:
        wb.close()
        if os.path.exists(temp):os.unlink(temp)
