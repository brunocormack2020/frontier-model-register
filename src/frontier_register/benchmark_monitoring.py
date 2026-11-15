"""Monitor supplementary evidence documents without extracting new scores.

A successful retrieval can confirm or challenge the source of a retained
observation. It never changes that observation's published value, model mapping,
original content hash, or review decision. A reviewer must acknowledge changed
raw evidence explicitly. Benchmark execution is outside this module.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from .benchmarks import BY_ID, ensure_benchmarks, is_active, iter_entries
from .collectors import CollectionError, fetch_sources
from .core import RegisterError, audit, utc_now


ALLOWED_HOSTS = (
    "www.aisi.gov.uk", "aisi.gov.uk", "arxiv.org", "github.com",
    "raw.githubusercontent.com", "meta-llama.github.io",
)
_FAILED = {"unavailable", "error", "parse_error"}
_SUCCESS = {"new", "changed", "unchanged"}
_WATCH_FIELDS = ("raw_sha256", "raw_path", "text_path", "checked_at", "url", "extraction_warning")


def _source_id(url: str, benchmark_id: str | None = None) -> str:
    prefix = "supplementary" if benchmark_id is None else "supp_" + benchmark_id
    return prefix + "_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def _failure_message(exc: Exception) -> str:
    if isinstance(exc, ImportError) and "socksio" in str(exc).lower():
        return ("SOCKS proxy support is unavailable because socksio is not installed. "
                "Use the company's approved httpx SOCKS dependency or a supported HTTPS proxy. "
                "Existing published findings were retained.")
    return f"Supplementary source collection failed ({type(exc).__name__}); existing findings were retained."


def refresh_supplementary_sources(
    state: dict, evidence_dir: Path, *, session=None, resolver=None,
    timeout=30, max_bytes=10_000_000,
) -> dict:
    """Check AISI and selected conditional sources, committing one audited update.

    ``changed_observations`` counts entries newly flagged or observed at a new
    changed hash in this run. Repeated checks of the same unreviewed document do
    not count as another change. A missing baseline requires source review but
    is labelled ``baseline_missing`` rather than a demonstrated source change.
    Already-required source review is cleared only by the separate review path.

    ``source_checks`` contains one check per source URL and affected benchmark;
    each URL is fetched once. Existing source errors are replaced on a later
    successful attempt. No selected sources means no network, state, or audit
    change. The caller is responsible for holding the register writer lock.
    """
    result = {"source_checks": [], "errors": [], "changed_observations": 0}
    draft = copy.deepcopy(state)
    groups: dict[str, list[dict]] = {}
    for kind, entry in iter_entries(draft):
        data = entry.get("data", {})
        bid = data.get("benchmark_id")
        if bid not in BY_ID:
            raise RegisterError("Supplementary evidence contains an unknown benchmark.")
        selected = kind == "aisi" or (BY_ID[bid]["scope"] in ("coding", "soc") and is_active(draft, bid))
        if not selected:
            continue
        url = data.get("source_url")
        if not isinstance(url, str) or not url:
            raise RegisterError("Supplementary evidence requires a source URL.")
        groups.setdefault(url, []).append(entry)
    if not groups:
        return result

    sources = [{"source_id": _source_id(url), "url": url} for url in groups]
    try:
        fetched = fetch_sources(sources, Path(evidence_dir), ALLOWED_HOSTS, session=session,
                                resolver=resolver, timeout=timeout, max_bytes=max_bytes)
    except (CollectionError, ImportError, OSError, ValueError) as exc:
        error = _failure_message(exc)
        fetched = [{**source, "status": "unavailable", "checked_at": utc_now(), "error": error}
                   for source in sources]
    by_id = {c.get("source_id"): c for c in fetched if isinstance(c, dict)}
    extension = ensure_benchmarks(draft)
    for url, entries in groups.items():
        source = {"source_id": _source_id(url), "url": url}
        check = copy.deepcopy(by_id.get(source["source_id"]) or {
            **source, "status": "unavailable", "checked_at": utc_now(),
            "error": "No collection result returned for the supplementary source.",
        })
        check.setdefault("url", url)
        check.setdefault("checked_at", utc_now())
        successful = check.get("status") in _SUCCESS and isinstance(check.get("raw_sha256"), str) and bool(check["raw_sha256"])
        if not successful:
            check["status"] = "unavailable"
            check.setdefault("error", "Supplementary source did not return confirmed content and a hash.")
        for bid in sorted({e["data"]["benchmark_id"] for e in entries}):
            scoped = {**check, "source_id": _source_id(url, bid), "benchmark_id": bid,
                      "monitor_kind": "supplementary_document", "numeric_results_updated": False}
            previous = extension["source_checks"].get(scoped["source_id"], {})
            if not successful:
                scoped["last_success"] = previous.get("last_success") or (
                    previous if previous and previous.get("status") not in _FAILED else None)
                result["errors"].append({"benchmark_id": bid, "url": url, "error": scoped["error"]})
            extension["source_checks"][scoped["source_id"]] = scoped
            result["source_checks"].append(copy.deepcopy(scoped))

        for entry in entries:
            previous_watch = entry.get("source_watch", {})
            watch = {k: check.get(k) for k in _WATCH_FIELDS}
            watch["url"] = url
            watch["fetch_status"] = check.get("status")
            if not successful:
                # Cached or previously watched bytes are historical. Never
                # compare them as if an unsuccessful request confirmed them.
                watch = {**copy.deepcopy(previous_watch), **{k: v for k, v in watch.items() if v is not None},
                         "status": "unavailable", "error": check["error"]}
            else:
                current_hash = check["raw_sha256"]
                baseline = entry.get("review", {}).get("source_sha256") or entry["data"].get("source_sha256")
                if baseline and baseline != current_hash:
                    watch["status"] = "changed"
                    if not entry.get("source_review_required") or previous_watch.get("raw_sha256") != current_hash:
                        result["changed_observations"] += 1
                    entry["source_review_required"] = True
                elif baseline:
                    watch["status"] = "unchanged"
                else:
                    watch["status"] = "baseline_missing"
                    watch["note"] = "No recorded source hash establishes continuity with the retained finding. Review this raw document."
                    entry["source_review_required"] = True
            entry["source_watch"] = watch

    audit(draft, "monitor_supplementary_sources", "automation", {
        "source_urls": list(groups), "source_checks": len(result["source_checks"]),
        "changed_observations": result["changed_observations"], "errors": result["errors"],
        "numeric_results_updated": False,
    })
    state.clear()
    state.update(draft)
    return result
