"""Published benchmark observations, explicit model mapping and evidence review.

No benchmark is executed here. Scores never become organization classifications.
"""
from __future__ import annotations

import copy
import math
import re
from urllib.parse import urlsplit

from .core import RegisterError, PROVIDERS, audit, digest, utc_now

REGISTRY = [
    {"id":"cybergym","name":"CyberGym","category":"Vulnerability reproduction","scope":"core","metrics":"Success rate; level and attempts required","direction":"Higher demonstrates greater capability","url":"https://www.cybergym.io/cybergym/","limit":"A reproduced vulnerability is not necessarily a working exploit. Keep task level, trial count, tools and memory setting."},
    {"id":"exploitgym","name":"ExploitGym","category":"Exploit generation","scope":"core","metrics":"Intended-vulnerability exploit successes by domain and mitigation setting","direction":"Higher demonstrates greater capability","url":"https://www.cybergym.io/exploitgym/","limit":"Submissions may cover different subsets. Do not assume 869 tasks were evaluated. Keep flag capture separate from intended-vulnerability success."},
    {"id":"cybergym_e2e","name":"CyberGym-E2E","category":"Discovery and repair","scope":"core","metrics":"Cumulative S1, S2, S3, S4; patch-only separately","direction":"Higher demonstrates discovery or defensive repair capability","url":"https://www.cybergym.io/cybergym-e2e/","limit":"S3 is discover-and-patch success under functionality tests. S4 checks the intended vulnerability. Separate 615/920 task sets and budgets."},
    {"id":"cyberbingym","name":"CyberBinGym","category":"Binary vulnerability recovery and exploitation","scope":"core","metrics":"Recovery and exploitation scored separately","direction":"Higher demonstrates greater capability","url":"https://www.cybergym.io/cyberbingym/","limit":"Main experiments used trusted access with safeguards disabled. Keep target hardening, source/PoC availability and budget."},
    {"id":"cve_bench","name":"CVE-Bench","category":"Web application exploitation","scope":"core","metrics":"Pass@1 for description-withheld and description-provided settings","direction":"Higher demonstrates greater capability","url":"https://cvebench.com/","limit":"Zero-day names the information setting on historical CVEs. Benchmark versions and task collections can differ."},
    {"id":"cybench","name":"Cybench","category":"Broad CTF capability","scope":"core","metrics":"Unguided, guided and subtask success separately","direction":"Higher demonstrates greater capability","url":"https://cybench.github.io/","limit":"Published results include different subsets and retry policies. Preserve corrections and harness versions."},
    {"id":"agentdojo","name":"AgentDojo","category":"Prompt-injection robustness","scope":"core","metrics":"Targeted ASR; benign utility; utility under attack","direction":"Lower ASR and higher utility are preferable","url":"https://agentdojo.spylab.ai/results/","limit":"The project does not present a fair leaderboard across differing attacks and defenses. Do not compare unmatched configurations."},
    {"id":"secure_code_instruct","name":"Secure Code — Instruct","category":"Secure code generation","scope":"coding","metrics":"Detected vulnerable-code percentage; counts by language","direction":"Lower vulnerable percentage is preferable","url":"https://meta-llama.github.io/PurpleLlama/CyberSecEval/docs/benchmarks/secure_code_generation","limit":"CyberSecEval 4 track. Detector results do not prove functional correctness or absence of all vulnerabilities."},
    {"id":"secure_code_autocomplete","name":"Secure Code — Autocomplete","category":"Secure code generation","scope":"coding","metrics":"Detected vulnerable-code percentage; counts by language","direction":"Lower vulnerable percentage is preferable","url":"https://meta-llama.github.io/PurpleLlama/CyberSecEval/docs/benchmarks/secure_code_generation","limit":"Keep separate from Instruct. Preserve detector, language, sample count and implementation."},
    {"id":"cybersoceval_malware","name":"CyberSOCEval — Malware Analysis","category":"Defensive analysis","scope":"soc","metrics":"Exact-set accuracy; Jaccard similarity","direction":"Higher is preferable for defensive utility","url":"https://meta-llama.github.io/PurpleLlama/CyberSecEval/docs/benchmarks/malware_analysis","limit":"CyberSecEval 4 track. Preserve input truncation, parsing errors and original-paper versus adaptation settings."},
    {"id":"cybersoceval_threat_intelligence","name":"CyberSOCEval — Threat Intelligence","category":"Defensive analysis","scope":"soc","metrics":"Exact-set accuracy; Jaccard similarity","direction":"Higher is preferable for defensive utility","url":"https://meta-llama.github.io/PurpleLlama/CyberSecEval/docs/benchmarks/threat_intelligence_reasoning","limit":"Preserve text/image modality, task count and implementation. This is not full SOC automation validation."},
    {"id":"aisi_cyber_range","name":"AISI cyber-range findings","category":"Long-horizon cyber capability","scope":"evidence","metrics":"Scenario-specific mean/max progress and full success when reported","direction":"Interpret only within the named scenario and setup","url":"https://www.aisi.gov.uk/blog/how-do-frontier-ai-agents-perform-in-multi-step-cyber-attack-scenarios","limit":"Separate evidence category, not one AISI score. Published 32-step corporate and 7-step ICS ranges had no active defenders."},
]
BY_ID = {row["id"]:row for row in REGISTRY}
CORE = [row["id"] for row in REGISTRY if row["scope"] == "core"]
_TRANSIENT = {"retrieved_at", "source_sha256", "source_revision"}
_KINDS = {"benchmark":"observations", "aisi":"aisi_findings"}


def ensure_benchmarks(state: dict, settings: dict | None = None) -> dict:
    if "benchmarks" not in state:
        cfg = settings or {}
        if any(type(cfg.get(k,False)) is not bool for k in ("coding_in_scope","soc_in_scope")):
            raise RegisterError("Conditional benchmark scope settings must be YAML booleans, not quoted strings.")
        state["benchmarks"] = {"schema_version":1,
            "scope":{"coding":bool(cfg.get("coding_in_scope",False)),"soc":bool(cfg.get("soc_in_scope",False))},
            "observations":{},"aisi_findings":{},"source_checks":{}}
    extension = state["benchmarks"]
    if extension.get("schema_version") != 1:
        raise RegisterError("Unsupported benchmark extension schema.")
    return extension


def is_active(state: dict, benchmark_id: str) -> bool:
    scope = BY_ID[benchmark_id]["scope"]
    return scope in ("core","evidence") or state.get("benchmarks",{}).get("scope",{}).get(scope,False)


def set_scope(state: dict, coding: bool, soc: bool, actor: str) -> None:
    if type(coding) is not bool or type(soc) is not bool or not actor.strip():
        raise RegisterError("Benchmark scope needs booleans and an actor.")
    extension = ensure_benchmarks(state)
    scope = {"coding":coding,"soc":soc}
    if extension["scope"] != scope:
        old = extension["scope"]
        extension["scope"] = scope
        audit(state,"set_benchmark_scope",actor,{"previous":old,"current":scope})


def _validated(raw: dict, kind: str) -> dict:
    if not isinstance(raw,dict):
        raise RegisterError("Each benchmark observation must be an object.")
    d = copy.deepcopy(raw)
    if kind == "aisi":
        d["benchmark_id"] = "aisi_cyber_range"
        if not d.get("scenario"):
            raise RegisterError("AISI findings require a named scenario.")
    if d.get("benchmark_id") not in BY_ID or (kind == "benchmark" and d["benchmark_id"] == "aisi_cyber_range"):
        raise RegisterError("Unknown benchmark, or AISI finding in the benchmark table.")
    if d.get("provider") not in PROVIDERS:
        raise RegisterError("Benchmark provider must be Anthropic, OpenAI or Google.")
    for key in ("reported_model","metric","source_url","source_result_id"):
        if not isinstance(d.get(key),str) or not d[key].strip():
            raise RegisterError(f"Benchmark observation requires {key}.")
    url = urlsplit(d["source_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise RegisterError("Benchmark source must be an HTTPS URL without credentials.")
    if d.get("model_id") is not None and (not isinstance(d["model_id"],str) or not d["model_id"].strip()):
        raise RegisterError("model_id must be the disclosed identifier or null.")
    value = d.get("value")
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value < 0:
        raise RegisterError("A scored observation needs a finite nonnegative numeric value; missing results are not zero.")
    unit = d.get("unit")
    if unit not in ("percent","fraction","count","score","steps"):
        raise RegisterError("Unsupported benchmark result unit.")
    if (unit == "percent" and value > 100) or (unit == "fraction" and value > 1):
        raise RegisterError("Benchmark percentage/fraction is outside its valid range.")
    for key in ("successes","total_tasks","attempts","repeats"):
        v = d.get(key)
        if v is not None and (isinstance(v,bool) or not isinstance(v,int) or v < 0 or (key != "successes" and v == 0)):
            raise RegisterError(f"{key} must be a positive integer (successes may be zero) or null.")
    if d.get("successes") is not None and d.get("total_tasks") is not None and d["successes"] > d["total_tasks"]:
        raise RegisterError("Successes cannot exceed the disclosed task count.")
    if "budget" in d and not isinstance(d["budget"],dict):
        raise RegisterError("Budget must be an object containing the reported limits.")
    if "conditions" in d and not isinstance(d["conditions"],dict):
        raise RegisterError("Conditions must be an object.")
    if len(str(d)) > 100_000:
        raise RegisterError("Observation exceeds the metadata size limit.")
    digest(d)  # Also rejects non-finite numbers nested in conditions.
    d.setdefault("retrieved_at",utc_now())
    d.setdefault("model_id",None)
    return d


def observation_id(data: dict, kind: str = "benchmark") -> str:
    return ("aisi_" if kind == "aisi" else "bench_") + digest({k:data.get(k) for k in
        ("benchmark_id","provider","reported_model","source_url","source_result_id","metric")})[:24]


def content_digest(data: dict) -> str:
    return digest({k:v for k,v in data.items() if k not in _TRANSIENT})


def _exact_match(state: dict, data: dict) -> str | None:
    if not data.get("model_id"):
        return None
    found = [r["record_id"] for r in state["records"] if r["vendor"]["Provider"] == data["provider"]
             and r["vendor"]["Exact Model ID"] == data["model_id"]]
    return found[0] if len(found) == 1 else None


def candidate_matches(state: dict, data: dict) -> list[dict]:
    """Display-only exact normalized name/version suggestions; never auto-approve a join."""
    def norm(s):
        return re.sub(r"[^a-z0-9]","",str(s).lower())
    label = norm(data["reported_model"])
    candidates = []
    for record in state["records"]:
        f = record["vendor"]
        if f["Provider"] != data["provider"]:
            continue
        names = {norm(f["Exact Model ID"]),norm(f"{f['Model Family']} {f['Version']}")}
        if label in names:
            candidates.append({"record_id":record["record_id"],"model_id":f["Exact Model ID"],
                               "basis":"Name/version suggestion only; exact snapshot correspondence requires review."})
    return candidates


def merge_observations(state: dict, observations: list[dict], source_checks: list[dict] | None = None,
                       actor: str = "automation", kind: str = "benchmark") -> dict:
    if kind not in _KINDS or not isinstance(observations,list) or not actor.strip():
        raise RegisterError("Invalid benchmark import kind, observations or actor.")
    prepared = [_validated(raw,kind) for raw in observations]
    keys = [observation_id(d,kind) for d in prepared]
    if len(keys) != len(set(keys)):
        raise RegisterError("Duplicate benchmark observation identity in one import.")
    result = copy.deepcopy(state)
    ext = ensure_benchmarks(result)
    target = ext[_KINDS[kind]]
    counts = {"new":0,"changed":0,"unchanged":0}
    for oid, data in zip(keys,prepared):
        sha = content_digest(data)
        existing = target.get(oid)
        if existing and existing["content_sha256"] == sha:
            existing["data"] = data
            existing["last_seen_at"] = data["retrieved_at"]
            counts["unchanged"] += 1
            continue
        rid = _exact_match(result,data)
        history = copy.deepcopy(existing.get("history",[])) if existing else []
        if existing:
            history.append({k:copy.deepcopy(v) for k,v in existing.items() if k != "history"})
            # Keep a reviewed mapping, while requiring a fresh review of corrected results.
            if existing.get("record_id") and not rid and not data.get("model_id"):
                rid = existing["record_id"]
        target[oid] = {"observation_id":oid,"data":data,"content_sha256":sha,"record_id":rid,
            "mapping_status":("exact_id" if data.get("model_id") and rid else "reviewed_label" if rid else "unmapped"),
            "mapping_basis":existing.get("mapping_basis") if existing else None,
            "review":{"status":"pending"},"history":history,
            "first_seen_at":existing["first_seen_at"] if existing else data["retrieved_at"],"last_seen_at":data["retrieved_at"]}
        counts["changed" if existing else "new"] += 1
    for check in source_checks or []:
        if not isinstance(check,dict) or not (check.get("source_id") or check.get("url") or check.get("source_url")):
            raise RegisterError("Source checks require an identifier or URL.")
        sid = check.get("source_id") or check.get("url") or check.get("source_url")
        previous = ext["source_checks"].get(sid,{})
        latest = copy.deepcopy(check)
        if check.get("status") in ("unavailable","error","parse_error"):
            latest["last_success"] = previous.get("last_success") or (previous if previous.get("status") not in ("unavailable","error","parse_error") else None)
        ext["source_checks"][sid] = latest
    audit(result,"collect_benchmark_results" if kind == "benchmark" else "import_aisi_findings",actor,{**counts,"observations":len(prepared),"source_checks":len(source_checks or [])})
    state.clear()
    state.update(result)
    return counts


def import_evidence(state: dict, bundle: dict, actor: str) -> dict:
    if not isinstance(bundle,dict) or bundle.get("schema_version") != 1:
        raise RegisterError("Benchmark evidence file requires schema_version 1.")
    result = copy.deepcopy(state)
    report = {}
    if "observations" in bundle:
        report["benchmarks"] = merge_observations(result,bundle["observations"],bundle.get("source_checks",[]),actor)
    if "aisi_findings" in bundle:
        report["aisi"] = merge_observations(result,bundle["aisi_findings"],actor=actor,kind="aisi")
    if not report:
        raise RegisterError("No observations or aisi_findings supplied.")
    if bundle.get("coverage_notes"):
        ensure_benchmarks(result)["coverage_notes"] = copy.deepcopy(bundle["coverage_notes"])
    state.clear()
    state.update(result)
    return report


def iter_entries(state: dict, kind: str | None = None):
    extension = state.get("benchmarks",{})
    for k, field in _KINDS.items():
        if kind is None or kind == k:
            for entry in extension.get(field,{}).values():
                yield k, entry


def make_benchmark_review_queue(state: dict) -> dict:
    items = []
    for kind, entry in iter_entries(state):
        if entry["review"]["status"] != "pending" and not entry.get("source_review_required"):
            continue
        d = entry["data"]
        items.append({"kind":kind,"observation_id":entry["observation_id"],"expected_content_sha256":entry["content_sha256"],
            "action":"pending","record_id":entry.get("record_id"),"mapping_basis":entry.get("mapping_basis") or "",
            "review_note":"","published_result":copy.deepcopy(d),"candidate_records":candidate_matches(state,d),
            "source_sha256":entry.get("source_watch",{}).get("raw_sha256"),"raw_evidence_reviewed":False,
            "source_watch":copy.deepcopy(entry.get("source_watch"))})
    return {"schema_version":1,"state_sha256":digest(state),"generated_at":utc_now(),
        "instructions":"Set action accept or reject and add review_note. Optional record_id links a same-provider model after review; a label-only mapping requires mapping_basis. Published values cannot be edited in this queue. Unmapped accepted evidence remains available without affecting a model decision.","items":items}


def apply_benchmark_review(state: dict, queue: dict, reviewer: str) -> dict:
    if not reviewer.strip() or queue.get("state_sha256") != digest(state):
        raise RegisterError("Benchmark review is stale or reviewer attribution is missing.")
    result = copy.deepcopy(state)
    ext = ensure_benchmarks(result)
    records = {r["record_id"]:r for r in result["records"]}
    seen, events = set(), []
    for item in queue.get("items",[]):
        kind, oid = item.get("kind"), item.get("observation_id")
        if kind not in _KINDS or oid not in ext[_KINDS[kind]] or (kind,oid) in seen:
            raise RegisterError("Unknown or duplicate benchmark review item.")
        seen.add((kind,oid))
        entry = ext[_KINDS[kind]][oid]
        if item.get("expected_content_sha256") != entry["content_sha256"]:
            raise RegisterError("Benchmark content hash does not match the review.")
        action = item.get("action","pending")
        if action == "pending":
            continue
        if action not in ("accept","reject") or not str(item.get("review_note","")).strip():
            raise RegisterError("Benchmark acceptance/rejection requires a review_note.")
        allowed = {"kind","observation_id","expected_content_sha256","action","record_id","mapping_basis","review_note","published_result","candidate_records","source_sha256","raw_evidence_reviewed","source_watch"}
        if set(item) - allowed or ("published_result" in item and item["published_result"] != entry["data"]):
            raise RegisterError("Review cannot change published values or introduce organization fields.")
        rid = item.get("record_id")
        basis = str(item.get("mapping_basis") or "").strip()
        if rid is not None:
            if rid not in records or records[rid]["vendor"]["Provider"] != entry["data"]["provider"]:
                raise RegisterError("Model mapping requires a known record of the same provider.")
            actual = records[rid]["vendor"]["Exact Model ID"]
            disclosed = entry["data"].get("model_id")
            if disclosed and disclosed != actual:
                raise RegisterError("A disclosed model ID cannot be mapped to a different model ID.")
            if not disclosed and len(basis) < 12:
                raise RegisterError("A label-only mapping requires an explicit mapping_basis; snapshot correspondence remains unverified.")
        entry["record_id"] = rid
        entry["mapping_status"] = "exact_id" if rid and entry["data"].get("model_id") else "reviewed_label" if rid else "unmapped"
        entry["mapping_basis"] = basis or ("Published identifier matches the registered ID; underlying checkpoint disclosure remains source-dependent." if rid else None)
        watch = entry.get("source_watch",{})
        if action == "accept" and entry.get("source_review_required"):
            if watch.get("status") in ("unavailable","error","parse_error") or not watch.get("raw_sha256") or item.get("source_sha256") != watch["raw_sha256"] or item.get("raw_evidence_reviewed") is not True:
                raise RegisterError("Changed supplementary evidence requires review of the current raw source and its exact hash.")
        entry["review"] = {"status":"accepted" if action == "accept" else "rejected","reviewer":reviewer,"at":utc_now(),"note":item["review_note"],"content_sha256":entry["content_sha256"],
                           "source_sha256":watch.get("raw_sha256") or entry["data"].get("source_sha256")}
        entry["source_review_required"] = False
        events.append({"kind":kind,"observation_id":oid,"record_id":rid,"action":action,"note":item["review_note"],"mapping_basis":basis})
    if not events:
        raise RegisterError("No completed benchmark reviews supplied.")
    audit(result,"review_benchmark_evidence",reviewer,{"items":events})
    return result


def evidence_digest(record: dict, state: dict) -> str:
    entries = [{"kind":kind,"id":e["observation_id"],"sha":e["content_sha256"],"mapping":e["mapping_status"],"basis":e.get("mapping_basis"),"reviewed_source_sha256":e["review"].get("source_sha256")}
        for kind,e in iter_entries(state) if e.get("record_id") == record["record_id"] and e["review"]["status"] == "accepted" and not e.get("source_review_required") and is_active(state,e["data"]["benchmark_id"])]
    return digest({"scope":state.get("benchmarks",{}).get("scope",{}),"reviewed_evidence":sorted(entries,key=lambda x:(x["kind"],x["id"]))})


def monitoring(record: dict, state: dict) -> str:
    if "benchmarks" not in state:
        return ""
    entries = [e for _,e in iter_entries(state) if e.get("record_id") == record["record_id"] and is_active(state,e["data"]["benchmark_id"])]
    messages = []
    pending = sum(e["review"]["status"] == "pending" or e.get("source_review_required",False) for e in entries)
    if pending:
        messages.append(f"Benchmark review required ({pending} results)")
    bids = {e["data"]["benchmark_id"] for e in entries}
    if any(c.get("status") in ("unavailable","error","parse_error") and c.get("benchmark_id") in bids for c in state["benchmarks"].get("source_checks",{}).values()):
        messages.append("Benchmark source unavailable: retained results may be stale")
    bound = record["metadata"].get("decision_benchmark_sha256")
    if record["organization"]["Organization Classification"] != "Pending classification" and ((bound is not None and bound != evidence_digest(record,state)) or (bound is None and entries)):
        messages.append("Organization decision requires benchmark reassessment")
    return "; ".join(messages)


def summary(state: dict) -> dict:
    entries = list(iter_entries(state))
    return {"benchmark_observations":sum(k == "benchmark" for k,_ in entries),
        "aisi_findings":sum(k == "aisi" for k,_ in entries),
        "pending_benchmark_reviews":sum(e["review"]["status"] == "pending" or e.get("source_review_required",False) for _,e in entries),
        "unmapped_results":sum(not e.get("record_id") for _,e in entries),
        "benchmark_source_errors":sum(s.get("status") in ("unavailable","error","parse_error") for s in state.get("benchmarks",{}).get("source_checks",{}).values()),
        "conditional_scope":state.get("benchmarks",{}).get("scope",{})}


def coverage(state: dict, record: dict, benchmark_id: str) -> str:
    if not is_active(state,benchmark_id):
        return "Conditional — scope not enabled"
    matches = [e for _,e in iter_entries(state) if e["data"]["benchmark_id"] == benchmark_id and e.get("record_id") == record["record_id"] and e["review"]["status"] != "rejected"]
    if matches:
        accepted = sum(e["review"]["status"] == "accepted" for e in matches)
        failed = any(s.get("benchmark_id") == benchmark_id and s.get("status") in ("unavailable","error","parse_error") for s in state.get("benchmarks",{}).get("source_checks",{}).values())
        return f"{len(matches)} results; {accepted} reviewed" + ("; source unavailable" if failed else "")
    candidates = [e for _,e in iter_entries(state) if e["data"]["benchmark_id"] == benchmark_id and not e.get("record_id") and e["review"]["status"] != "rejected" and any(c["record_id"] == record["record_id"] for c in candidate_matches(state,e["data"]))]
    if candidates:
        return f"{len(candidates)} name/version matches; mapping unconfirmed"
    checks = list(state.get("benchmarks",{}).get("source_checks",{}).values())
    relevant = [s for s in checks if s.get("benchmark_id") == benchmark_id]
    if any(s.get("status") in ("unavailable","error","parse_error") for s in relevant):
        return "Source unavailable; no linked result"
    return "No linked published result"
