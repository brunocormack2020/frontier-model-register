"""Explicit commands for import, refresh, review, export and publication."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import shutil
import sys
from pathlib import Path

import yaml

from .core import (StateLock, RegisterError, HEADERS, PROVIDERS, atomic_json, read_json,
                   import_workbook, load_state, save_state, collect_source_catalog, apply_fetch_results,
                   make_review_queue, apply_review, apply_organization_decisions, merge_discovery,
                   audit, digest, monitoring, utc_now)
from .collectors import fetch_sources, discover_models, CollectionError, DiscoveryError
from .render import export_excel, export_html, confluence_fragment
from .confluence import ConfluenceConfig, ConfluencePublisher, ConfluenceError, managed_section
from .benchmarks import (ensure_benchmarks, import_evidence, merge_observations,
                         make_benchmark_review_queue, apply_benchmark_review, set_scope,
                         summary as benchmark_summary, evidence_digest)


def configuration(path: Path) -> dict:
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RegisterError("Cannot read configuration YAML.") from exc
    if not isinstance(cfg, dict) or cfg.get("schema_version") != 1:
        raise RegisterError("Configuration requires schema_version: 1.")
    root = path.resolve().parent
    cfg["work_path"] = (root / cfg.get("work_dir", "work")).resolve()
    cfg["output_path"] = (root / cfg.get("output_dir", "out")).resolve()
    cfg["config_root"] = root
    if not isinstance(cfg.get("allowed_source_hosts"), list) or not cfg["allowed_source_hosts"]:
        raise RegisterError("Configure a nonempty allowed_source_hosts list.")
    for key in ("enabled", "coding_in_scope", "soc_in_scope", "refresh_with_sources"):
        if key in cfg.get("benchmarks",{}) and type(cfg["benchmarks"][key]) is not bool:
            raise RegisterError(f"benchmarks.{key} requires a YAML boolean.")
    return cfg


def _catalog(state: dict, cfg: dict) -> list[dict]:
    return collect_source_catalog(state, cfg.get("watch_sources", []), cfg["allowed_source_hosts"], cfg.get("include_prior_sources", False))


def _render(state: dict, cfg: dict) -> dict:
    out, work = cfg["output_path"], cfg["work_path"]
    out.mkdir(parents=True, exist_ok=True)
    fragment = confluence_fragment(state, cfg.get("confluence", {}).get("include_prior_summary", True))
    export_excel(state, work / "template.xlsx", out / "model_register.xlsx")
    export_html(fragment, out / "register_preview.html")
    (out / "confluence_fragment.html").write_text(fragment, encoding="utf-8")
    (out / "confluence_initial_section.html").write_text(managed_section(fragment), encoding="utf-8")
    atomic_json(out / "review_queue.json", make_review_queue(state))
    atomic_json(out / "register.json", state)
    if "benchmarks" in state:
        atomic_json(out / "benchmark_review_queue.json", make_benchmark_review_queue(state))
        atomic_json(out / "benchmark_results.json", {"schema_version":1, **state["benchmarks"]})
    summary = {"state_version": state["state_version"], "records": len(state["records"]),
               "current_records": sum(r["sheet"] == "Model Register" for r in state["records"]),
               "pending_review_records": len(make_review_queue(state)["items"]),
               "unavailable_sources": sum(s["status"] == "unavailable" for s in state["source_checks"].values()),
               "output_dir": str(out)}
    if "benchmarks" in state:
        summary.update(benchmark_summary(state))
    atomic_json(out / "run_summary.json", summary)
    return summary


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Maintain vendor evidence and organization decisions for a frontier model register.")
    p.add_argument("--config", type=Path, default=Path("config.yaml"), help="YAML configuration; paths are relative to its directory")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Import the supplied workbook once; refuses to overwrite existing state")
    init.add_argument("--workbook", type=Path, required=True)
    refresh = sub.add_parser("refresh", help="Read public sources, create review queue and regenerate views")
    refresh.add_argument("--discover", action="store_true", help="Also query enabled provider model catalogs using environment credentials")
    refresh.add_argument("--source-id", action="append", help="Fetch only selected source IDs for a targeted check")
    refresh.add_argument("--benchmarks", action="store_true", help="Also collect the configured public benchmark results")
    discover = sub.add_parser("discover", help="Observe configured account-visible vendor catalogs; never retire absent models")
    discover.add_argument("--provider", choices=PROVIDERS, action="append")
    sub.add_parser("export", help="Regenerate Excel, JSON and Confluence HTML without network access")
    sub.add_parser("status", help="Print record and source status counts")
    sources = sub.add_parser("sources", help="List configured source identifiers and URLs")
    review = sub.add_parser("apply-review", help="Apply attributed vendor review; organization fields cannot be changed")
    review.add_argument("--file", type=Path, required=True)
    review.add_argument("--reviewer", required=True)
    organization_template = sub.add_parser("organization-template", help="Create a organization-decision request bound to the current state")
    organization_template.add_argument("--record-id", action="append", required=True)
    organization_template.add_argument("--output", type=Path, required=True)
    organization = sub.add_parser("organization-update", help="Explicitly record organization decisions or administrative deployment changes")
    organization.add_argument("--file", type=Path, required=True)
    organization.add_argument("--actor", required=True)
    add = sub.add_parser("add-model", help="Register a vendor model manually when API catalog access is unavailable")
    add.add_argument("--provider", choices=PROVIDERS, required=True)
    add.add_argument("--model-id", required=True)
    add.add_argument("--display-name")
    sub.add_parser("benchmark-refresh", help="Collect published benchmark scores; never execute the benchmarks")
    bi = sub.add_parser("benchmark-import", help="Import attributed published results and AISI evidence as pending review")
    bi.add_argument("--file",type=Path,required=True)
    bi.add_argument("--actor",default="operator")
    br = sub.add_parser("benchmark-review", help="Review results and explicit model mappings without changing organization decisions")
    br.add_argument("--file",type=Path,required=True)
    br.add_argument("--reviewer",required=True)
    bs = sub.add_parser("benchmark-scope", help="Select conditional coding/SOC evidence for this register")
    bs.add_argument("--coding",choices=("on","off"))
    bs.add_argument("--soc",choices=("on","off"))
    bs.add_argument("--actor",required=True)
    publish = sub.add_parser("publish", help="Preview an update to a configured existing Confluence page; no write by default")
    publish.add_argument("--apply", action="store_true", help="Write the managed section after retrieving the current page version")
    return p


def _discover(state: dict, cfg: dict, selected: list[str] | None = None) -> list[dict]:
    outcomes = []
    configuration = cfg.get("discovery", {})
    for provider in selected or PROVIDERS:
        settings = configuration.get(provider, {})
        if not settings.get("enabled", False) and not selected:
            continue
        try:
            models = discover_models(provider, timeout=cfg.get("http_timeout_seconds", 30))
            added = merge_discovery(state, provider, models, settings.get("prefixes", []))
            outcomes.append({"provider": provider, "seen": len(models), "added": added, "status": "succeeded"})
        except DiscoveryError as exc:
            error = {"provider": provider, "status": "unavailable", "error": str(exc)}
            outcomes.append(error)
            state["discovery_runs"].append(error)
            audit(state, "discovery_failed", "automation", error)
    return outcomes


def _benchmark_refresh(state: dict, cfg: dict) -> dict:
    from .benchmark_collectors import collect_benchmarks
    from .benchmark_monitoring import refresh_supplementary_sources
    settings = cfg.get("benchmarks",{})
    ensure_benchmarks(state,settings)
    result = collect_benchmarks(settings,cfg["work_path"] / "benchmark_evidence",
        timeout=cfg.get("http_timeout_seconds",30),max_bytes=cfg.get("max_source_bytes",10_000_000))
    counts = merge_observations(state,result["observations"],result["source_checks"])
    supplementary = refresh_supplementary_sources(state,cfg["work_path"] / "benchmark_evidence" / "supplementary",
        timeout=cfg.get("http_timeout_seconds",30),max_bytes=cfg.get("max_source_bytes",10_000_000)) if settings.get("enabled",True) else {"errors":[],"changed_observations":0}
    return {**counts,"source_errors":result["errors"]+supplementary["errors"],
            "supplementary_source_changes":supplementary["changed_observations"]}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        cfg = configuration(args.config)
        work = cfg["work_path"]
        state_path = work / "register.json"
        with StateLock(work):
            if args.command == "init":
                if state_path.exists():
                    raise RegisterError("State already exists. init does not overwrite it. Use review/update commands or a new work_dir.")
                state = import_workbook(args.workbook)
                _catalog(state, cfg)
                settings = cfg.get("benchmarks",{})
                if settings.get("enabled",False):
                    ensure_benchmarks(state,settings)
                    for seed in settings.get("seed_files",[]):
                        import_evidence(state,read_json(cfg["config_root"] / seed),"bundled_public_evidence")
                work.mkdir(parents=True, exist_ok=True)
                shutil.copy2(args.workbook, work / "template.xlsx")
                save_state(state_path, state)
                print(json.dumps(_render(state, cfg), indent=2))
                return 0
            state = load_state(state_path)
            if args.command == "status":
                print(json.dumps({"state_version": state["state_version"], "state_sha256": digest(state), "records": len(state["records"]),
                                  "pending_reviews": len(make_review_queue(state)["items"]), "sources": len(state["sources"]),
                                  "records_requiring_attention": sum("required" in monitoring(r,state)[0] or "unavailable" in monitoring(r,state)[0] for r in state["records"]),
                                  **benchmark_summary(state)}, indent=2))
                return 0
            if args.command == "sources":
                print(json.dumps(list(state["sources"].values()), indent=2))
                return 0
            if args.command in ("refresh", "discover"):
                discovery = _discover(state, cfg, getattr(args,"provider",None)) if args.command == "discover" or args.discover else []
                sources = _catalog(state, cfg)
                results = []
                if args.command == "refresh":
                    if args.source_id:
                        known = {s["source_id"] for s in sources}
                        if set(args.source_id) - known:
                            raise RegisterError("Unknown source-id. Run sources to list available IDs.")
                        sources = [s for s in sources if s["source_id"] in args.source_id]
                    results = fetch_sources(sources, work / "evidence", cfg["allowed_source_hosts"],
                                            timeout=cfg.get("http_timeout_seconds",30), max_bytes=cfg.get("max_source_bytes",10_000_000))
                    apply_fetch_results(state, results)
                bench_report = None
                if args.command == "refresh" and (args.benchmarks or (not args.source_id and cfg.get("benchmarks",{}).get("enabled",False) and cfg["benchmarks"].get("refresh_with_sources",True))):
                    bench_report = _benchmark_refresh(state,cfg)
                save_state(state_path, state)
                summary = _render(state, cfg)
                summary["discovery"] = discovery
                summary["benchmark_refresh"] = bench_report
                print(json.dumps(summary, indent=2))
                return 2 if any(r["status"] == "unavailable" for r in results+discovery) or (bench_report and bench_report["source_errors"]) else 0
            if args.command == "apply-review":
                state = apply_review(state, read_json(args.file), args.reviewer)
                _catalog(state, cfg)
            elif args.command == "organization-template":
                by_id = {r["record_id"]:r for r in state["records"]}
                if set(args.record_id) - set(by_id):
                    raise RegisterError("Unknown record ID.")
                atomic_json(args.output, {"schema_version":1,"state_sha256":digest(state),"items":[{
                    "record_id":rid,"confirm_decision":False,"expected_vendor_sha256":digest(by_id[rid]["vendor"]),
                    "expected_benchmark_sha256":evidence_digest(by_id[rid],state) if "benchmarks" in state else None,"updates":{}
                } for rid in args.record_id]})
                print(f"Wrote {args.output}")
                return 0
            elif args.command == "organization-update":
                state = apply_organization_decisions(state, read_json(args.file), args.actor)
            elif args.command == "add-model":
                settings = cfg.get("discovery",{}).get(args.provider,{})
                model = {"provider":args.provider,"model_id":args.model_id,"display_name":args.display_name or args.model_id,
                         "description":"Manually registered identifier. Vendor evidence requires review."}
                if not args.model_id.startswith(tuple(settings.get("prefixes",[]))):
                    raise RegisterError("Model identifier is outside the configured prefix scope.")
                merge_discovery(state,args.provider,[model],settings["prefixes"])
                _catalog(state,cfg)
            elif args.command == "benchmark-refresh":
                report = _benchmark_refresh(state,cfg)
                save_state(state_path,state)
                print(json.dumps({**_render(state,cfg),"benchmark_refresh":report},indent=2))
                return 2 if report["source_errors"] else 0
            elif args.command == "benchmark-import":
                ensure_benchmarks(state,cfg.get("benchmarks",{}))
                import_evidence(state,read_json(args.file),args.actor)
            elif args.command == "benchmark-review":
                state = apply_benchmark_review(state,read_json(args.file),args.reviewer)
            elif args.command == "benchmark-scope":
                scope = ensure_benchmarks(state,cfg.get("benchmarks",{}))["scope"]
                if args.coding is None and args.soc is None:
                    raise RegisterError("Select --coding on/off and/or --soc on/off.")
                set_scope(state,args.coding == "on" if args.coding is not None else scope["coding"],
                          args.soc == "on" if args.soc is not None else scope["soc"],args.actor)
            elif args.command == "publish":
                settings = cfg.get("confluence",{})
                if not settings.get("enabled",False):
                    raise RegisterError("Confluence is disabled. Configure an existing page and enable it before a publish preview.")
                allowed = {"mode","base_url","page_id","token_env","username_env","auth_kind","timeout_seconds"}
                conf = ConfluenceConfig(**{k:v for k,v in settings.items() if k in allowed})
                fragment = confluence_fragment(state,settings.get("include_prior_summary",True))
                result = ConfluencePublisher(conf).publish(fragment,dry_run=not args.apply)
                preview = cfg["output_path"] / "confluence_proposed_page.html"
                preview.parent.mkdir(parents=True,exist_ok=True)
                preview.write_text(result.proposed_body,encoding="utf-8")
                report = {k:v for k,v in asdict(result).items() if k != "proposed_body"}
                if result.status == "published":
                    # Publication history must not change the content revision or invalidate a pending evidence review.
                    history_path = work / "publication_history.json"
                    history = read_json(history_path) if history_path.exists() else {"schema_version":1,"events":[]}
                    history["events"].append({"at":utc_now(),"actor":"automation","state_sha256":digest(state),**report})
                    atomic_json(history_path,history)
                print(json.dumps(report,indent=2))
                return 0
            elif args.command == "export":
                print(json.dumps(_render(state,cfg),indent=2))
                return 0
            save_state(state_path,state)
            print(json.dumps(_render(state,cfg),indent=2))
            return 0
    except (RegisterError, CollectionError, DiscoveryError, ConfluenceError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}",file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
