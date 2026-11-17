"""Evidence-bound, human-reviewed model identity relationships.

Discovery supplies observations, not checkpoint equivalence. This module never
changes a model, benchmark mapping, organization decision, or intelligence claim.
No transitive inference: a confirmed A~B and B~C does not establish A~C.
"""
from __future__ import annotations

import copy
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


def _api_hf_suggestion(a:dict,b:dict):
    """Only emit conservative named-developer API/HF suggestions; never equivalence."""
    if a["source_type"] == "huggingface_repository":
        a,b=b,a
    if a["source_type"] != "developer_api" or b["source_type"] != "huggingface_repository":
        return None
    # An API catalogue can host third-party models; matching names only prompts review.
    expected={"Mistral":"mistralai"}.get(a["provider"])
    if not expected or b["provider"] != expected:
        return None
    hf_name=b["label"].split("/",1)[-1]
    api_name=a["details"].get("api_model_id",a["label"])
    if _norm(hf_name)==_norm(api_name) and _norm(hf_name):
        return "Exact normalized model/repository name in Mistral catalogue and curated publisher namespace; checkpoint NOT verified"
    aliases=a["details"].get("aliases",[])
    if isinstance(aliases,list) and any(_norm(hf_name)==_norm(alias) for alias in aliases if isinstance(alias,str)):
        return "Name matches a mutable API alias and curated publisher repository; alias/checkpoint NOT verified"
    return None


def suggestions(nodes:dict,identity_state:dict) -> list[dict]:
    """Discovery-only hints; deterministic and bounded, never approved joins."""
    api=[];hf=[]
    for node in nodes.values():
        if node["source_type"]=="developer_api": api.append(node)
        elif node["source_type"]=="huggingface_repository": hf.append(node)
    hf_by_name={}
    for item in hf:
        if item["provider"]=="mistralai":
            n=_norm(item["label"].split("/",1)[-1])
            hf_by_name.setdefault(n,[]).append(item)
    pairs=[]
    for candidate in sorted(api,key=lambda x:x["node_id"]):
        if candidate["provider"]!="Mistral": continue
        terms=[candidate["details"].get("api_model_id",candidate["label"])]
        aliases=candidate["details"].get("aliases",[])
        if isinstance(aliases,list):terms+=aliases[:60]
        matches={}
        for term in terms:
            if not isinstance(term,str) or not _norm(term):continue
            for found in hf_by_name.get(_norm(term),[]):
                reason=_api_hf_suggestion(candidate,found)
                if reason:matches[found["node_id"]]=(found,reason)
        for item,reason in sorted(matches.values(),key=lambda t:t[0]["node_id"]):
            left,right,pair_id=_pair(candidate["node_id"],item["node_id"],nodes)
            saved=identity_state["links"].get(pair_id)
            pairs.append({"pair_id":pair_id,"left_id":left,"right_id":right,
                          "reason":reason,"suggested_relation":"same_model_version (review required)",
                          "link_status":link_status(saved,nodes) if saved else "unreviewed",
                          "evidence_hashes":{left:nodes[left]["evidence_sha256"],right:nodes[right]["evidence_sha256"]}})
            if len(pairs)>MAX_SUGGESTIONS:
                raise IntelligenceError("Excessive identity suggestions; narrow curated discovery")
    return pairs


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


def _item(nodes:dict,left_id:str,right_id:str,reason:str="Manual reconciliation request") -> dict:
    left,right,pair_id=_pair(left_id,right_id,nodes)
    return {"pair_id":pair_id,"left_id":left,"right_id":right,"reason":reason,
            "evidence_hashes":{left:nodes[left]["evidence_sha256"],right:nodes[right]["evidence_sha256"]},
            "action":"pending","relation":"same_model_version","evidence_url":"",
            "proof_kind":"","proof_reference":"","review_note":""}


def review_queue(nodes:dict,identity_state:dict) -> dict:
    known=identity_state["links"]
    items=[]
    for s in suggestions(nodes,identity_state):
        # Re-review only unresolved, revoked, or stale links. Do not replay confirmed decisions.
        if s["link_status"] in ("confirmed","rejected"):continue
        items.append(_item(nodes,s["left_id"],s["right_id"],s["reason"]))
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
    return {"identity_state_version":identity_state["state_version"],"candidate_and_register_nodes":len(nodes),
            "suggested_pairs":len(suggestions(nodes,identity_state)),
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
        ("Identity Suggestions",["Pair ID","Left Model / Source","Right Model / Source","Match Basis","Status","Left Evidence SHA","Right Evidence SHA"],
         [[s["pair_id"],nodes[s["left_id"]]["label"],nodes[s["right_id"]]["label"],s["reason"],s["link_status"],
           s["evidence_hashes"][s["left_id"]],s["evidence_hashes"][s["right_id"]]] for s in hints]),
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
            ws.column_dimensions[get_column_letter(index)].width=48 if index in (2,3,4,8,10,11) else 28
        ws.freeze_panes="C2";ws.auto_filter.ref=f"A1:{get_column_letter(len(headers))}{max(2,ws.max_row)}"
    fd,temp=tempfile.mkstemp(prefix=".identity-excel-",suffix=".xlsx",dir=workbook_path.parent)
    os.close(fd)
    try:
        wb.save(temp)
        os.replace(temp,workbook_path)
    finally:
        wb.close()
        if os.path.exists(temp):os.unlink(temp)
