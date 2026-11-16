"""Collect published benchmark observations; never execute benchmark workloads.

All parsers are data-only. They do not execute page JavaScript, follow report
links, run repositories, call an LLM, classify models, or modify organization decisions.
Scores remain attached to their reported model, agent and evaluation settings.
The defaults cover seven official sources. ``settings.sources`` may override a
source URL by benchmark ID; overrides must still pass the exact public-host
allowlist in ``settings.allowed_hosts`` (or DEFAULT_ALLOWED_HOSTS).

The CSV/JSON/YAML adapters are intentionally strict about schema and numeric
ranges. An unavailable or unexpectedly empty source is an error, not evidence
that a model scored zero or disappeared. Unknown task counts remain unknown.
"""
from __future__ import annotations

import csv
from datetime import date
import hashlib
from html.parser import HTMLParser
import io
import json
import math
from pathlib import Path
import re
from typing import Any

import yaml

from .collectors import CollectionError, fetch_sources, _now


CORE_BENCHMARKS = ("cybergym", "exploitgym", "cybergym_e2e", "cyberbingym", "cve_bench", "cybench", "agentdojo")
DEFAULT_ALLOWED_HOSTS = ("www.cybergym.io", "cybench.github.io", "agentdojo.spylab.ai", "api.github.com", "raw.githubusercontent.com")
DEFAULT_SOURCES = {
    "cybergym": "https://www.cybergym.io/assets/data/cybergym.json",
    "exploitgym": "https://www.cybergym.io/assets/data/exploitgym.json",
    "cybergym_e2e": "https://www.cybergym.io/assets/data/cybergym-e2e.json",
    "cyberbingym": "https://www.cybergym.io/assets/data/cyberbingym.json",
    "cve_bench": "https://api.github.com/repos/uiuc-kang-lab/cvebench.com/git/trees/master?recursive=1",
    "cybench": "https://cybench.github.io/data/leaderboard.csv",
    "agentdojo": "https://agentdojo.spylab.ai/results/",
}
LEADERBOARDS = {
    "cybergym": "https://www.cybergym.io/cybergym/",
    "exploitgym": "https://www.cybergym.io/exploitgym/",
    "cybergym_e2e": "https://www.cybergym.io/cybergym-e2e/",
    "cyberbingym": "https://www.cybergym.io/cyberbingym/",
    "cve_bench": "https://cvebench.com/",
    "cybench": "https://cybench.github.io/",
    "agentdojo": "https://agentdojo.spylab.ai/results/",
}
_CATEGORIES = {"cybergym": "dual_use", "cybergym_e2e": "dual_use", "agentdojo": "robustness"}


class BenchmarkParseError(ValueError):
    """A published source cannot be interpreted without guessing."""


def _text(value: Any, *, limit: int = 20000) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise BenchmarkParseError("Expected a bounded text field")
    return value.strip() or None


def _number(value: Any, *, percent=False, fraction=False, integer=False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BenchmarkParseError("Expected a finite numeric score")
    if value < 0 or (percent and value > 100) or (fraction and value > 1):
        raise BenchmarkParseError("Score is outside its documented range")
    if integer and value != int(value):
        raise BenchmarkParseError("Expected an integer count")
    return int(value) if integer else value


def _optional_count(value: Any) -> int | None:
    return None if value is None else _number(value, integer=True)


def _date(value: Any) -> str | None:
    value = _text(value)
    if value is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}", value):
        date.fromisoformat(value + "-01")
        return value  # Preserve month precision. Never invent a day.
    date.fromisoformat(value)
    return value


def _provider(model: str) -> str | None:
    """Identify vendor from a reported single-model label, never its agent name."""
    if re.search(r"(?i)multi.model|fine.?tun|distill|\s\+\s|,", model):
        return None
    if re.match(r"(?i)^claude(?:\b|[-_])", model):
        return "Anthropic"
    if re.match(r"(?i)^gemini(?:\b|[-_])", model):
        return "Google"
    if re.match(r"(?i)^(?:openai\s+)?(?:gpt(?:\b|[-_])|o[1-9](?:\b|[-_]))", model):
        return "OpenAI"
    return None


def _base(benchmark: str, row: dict, source_url: str, raw_sha: str, retrieved: str) -> dict | None:
    model = _text(row.get("model"))
    if not model:
        raise BenchmarkParseError("A result omitted its model label")
    provider = _provider(model)
    if not provider:
        return None
    original = _text(row.get("source_url"))
    return {
        "benchmark_id": benchmark, "provider": provider, "reported_model": model,
        "model_id": None, "agent": _text(row.get("agent") or row.get("harness")),
        "metric": "", "value": None, "unit": "percent", "successes": None,
        "total_tasks": None, "attempts": None, "benchmark_version": _text(row.get("version")),
        "dataset_subset": None, "setting": None, "budget": {}, "safeguards": None,
        "mitigations": None, "source_url": source_url, "source_result_id": "",
        "source_revision": None, "source_sha256": raw_sha, "evaluation_date": None,
        "published_date": _date(row.get("date")), "retrieved_at": retrieved,
        "evaluator": _text(row.get("source")), "notes": "",
        "metric_direction": "capability", "category": _CATEGORIES.get(benchmark, "offensive_capability"),
        "conditions": {"leaderboard_url": LEADERBOARDS[benchmark], "original_report_url": original,
                       "reported_date_semantics": "Leaderboard date; precise evaluation date not specified",
                       "reported_model_label": model},
    }


def _finish(base: dict, metric: str, value: Any, *, unit="percent", **updates) -> dict:
    result = {**base, "metric": metric, "value": _number(value, percent=unit == "percent", fraction=unit == "fraction", integer=unit == "count"), "unit": unit, **updates}
    # Identity excludes score, retrieval date and explanatory notes. A corrected
    # score for the same published run is a revision, not an unrelated result.
    identity = {k: result.get(k) for k in ("benchmark_id", "reported_model", "agent", "metric", "benchmark_version", "dataset_subset", "total_tasks", "attempts", "setting", "budget", "safeguards", "mitigations", "published_date", "evaluation_date", "source_url", "evaluator")}
    identity["variant"] = result["conditions"].get("variant_key")
    identity["features"] = result["conditions"].get("features")
    identity["original_report_url"] = result["conditions"].get("original_report_url")
    result["source_result_id"] = result["benchmark_id"] + ":" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    return result


def _rows(data: dict, key: str) -> list[dict]:
    rows = data.get(key)
    if not isinstance(rows, list) or not rows or len(rows) > 20000 or not all(isinstance(r, dict) for r in rows):
        raise BenchmarkParseError("Source results array missing, empty, oversized or invalid")
    flattened = []
    for row in rows:
        if row.get("hidden"):
            continue
        children = row.get("subRows", [])
        if not isinstance(children, list) or not all(isinstance(s, dict) for s in children):
            raise BenchmarkParseError("Invalid nested result variants")
        parent = {k: v for k, v in row.items() if k != "subRows"}
        flattened.append(parent)
        # Inherit the submission context, not another variant's outcomes or
        # measured usage. A child omitting a count means it is not reported.
        measurements = {"score_10", "score_x1", "patch_only", "s1", "s2", "s3", "s4", "poc", "exploit", "on_target", "userspace", "v8", "kernel", "flag_captured", "mitigated", "metrics", "metrics_success", "cost", "time_h", "llm_calls", "tool_calls"}
        context = {k: v for k, v in parent.items() if k not in measurements}
        for child in children:
            if not child.get("hidden"):
                inherited = dict(context)
                if child.get("model") and child["model"] != parent.get("model"):
                    inherited.pop("model_release_date", None)
                flattened.append({**inherited, **child})
    return flattened


def _observatory(benchmark: str, data: dict, url: str, sha: str, checked: str) -> list[dict]:
    out = []
    for row in _rows(data, "level1" if benchmark == "cybergym" else "results"):
        # Match the public CyberGym leaderboard's visible primary single-trial
        # Level 1 rows. Undocumented score_x1 fields are deliberately not used.
        if benchmark == "cybergym" and row.get("trials") != 1:
            continue
        base = _base(benchmark, row, url, sha, checked)
        if base is None:
            continue
        base["conditions"].update({"focus": row.get("focus"), "features": row.get("features"),
                                    "model_release_date": row.get("model_release_date")})
        if benchmark == "cybergym":
            base.update(setting="Level 1; vulnerability description and unpatched source", attempts=1,
                        dataset_subset="Level 1; per-submission task list/count not disclosed by this JSON")
            base["conditions"]["source_score_field"] = "score_10"
            base["notes"] = "Published single-trial vulnerability-reproduction result. The legacy score_10 field does not establish a universal ten-dollar budget."
            value = _number(row.get("score_10"), fraction=True)
            out.append(_finish(base, "vulnerability_reproduction_success", round(value * 100, 10)))
        elif benchmark == "cybergym_e2e":
            total = _optional_count(row.get("task_set"))
            if not total:
                raise BenchmarkParseError("CyberGym-E2E task_set is missing or zero")
            base.update(total_tasks=total, dataset_subset=f"Reported {total}-task set", budget={"reported_limit": _text(row.get("budget"))})
            base["notes"] = "S1-S4 are cumulative. S3 is discover-and-patch success under functionality tests; S4 checks the intended ground-truth vulnerability. Patch-only is a separate task with supplied PoC and crash log."
            for key in ("patch_only", "s1", "s2", "s3", "s4"):
                if row.get(key) is not None:
                    setting = "Patch-only; supplied PoC and crash log" if key == "patch_only" else "End-to-end; ground-truth vulnerability withheld"
                    out.append(_finish(base, key, row[key], setting=setting, metric_direction="higher_is_better" if key == "patch_only" else "capability"))
        elif benchmark == "cyberbingym":
            total = _optional_count(data.get("instances", {}).get("total"))
            if not total:
                raise BenchmarkParseError("CyberBinGym total task count is missing")
            base.update(total_tasks=total, dataset_subset=f"Full reported {total}-task set", setting="Stripped vulnerable and patched binaries; no supplied source or PoC",
                        budget={"nominal_llm_call_limit": 1000}, safeguards="Main experiments: trusted access; safeguards disabled", mitigations="Main benchmark setting; hardened follow-up is a different evaluation")
            base["conditions"].update({"average_cost_usd": row.get("cost"), "average_time_hours": row.get("time_h"), "average_llm_calls": row.get("llm_calls"), "average_tool_calls": row.get("tool_calls"), "methodology_url": LEADERBOARDS[benchmark]})
            base["notes"] = "Recovery and exploitation are independent outcomes. Published main setting uses a nominal 1,000-call budget; averages are usage, not limits. Month-only dates retain their precision."
            for key, metric in (("poc", "vulnerability_recovery"), ("exploit", "exploitation_success")):
                if row.get(key) is not None:
                    out.append(_finish(base, metric, row[key]))
        else:
            base.update(setting=_text(row.get("eval_note")), budget={"reported_limit": _text(row.get("eval_note"))},
                        dataset_subset="Submission-specific task coverage not disclosed by this JSON",
                        safeguards="Trusted-access research evaluation; exact per-submission safeguards require original report")
            base["conditions"].update({"benchmark_total_tasks": data.get("instances", {}).get("total"), "reported_usage_averages": row.get("metrics"), "reported_usage_on_success": row.get("metrics_success"), "source_note": row.get("_note")})
            base["notes"] = "Counts are not percentages. Submission coverage may be a subset of the benchmark; denominator is deliberately unknown. Intended-vulnerability outcomes and all flag captures are separate. " + (row.get("_note") or "")
            if "derived" in str(row.get("_note", "")).lower() or "approx" in str(row.get("_note", "")).lower():
                base["conditions"]["reported_counts_are_approximate"] = True
            found = False
            for key in ("on_target", "userspace", "v8", "kernel", "flag_captured"):
                if row.get(key) is not None:
                    metric = "all_flag_captures" if key == "flag_captured" else "intended_vulnerability_exploits" if key == "on_target" else f"intended_exploits_{key}"
                    out.append(_finish(base, metric, row[key], unit="count", mitigations="Disabled in primary leaderboard run"))
                    found = True
            hardened = row.get("mitigated")
            if hardened is not None:
                if not isinstance(hardened, dict):
                    raise BenchmarkParseError("Invalid mitigated results")
                for domain in ("userspace", "v8", "kernel"):
                    if hardened.get(domain) is not None:
                        out.append(_finish(base, f"intended_exploits_{domain}", hardened[domain], unit="count", mitigations="Enabled; domain-specific defenses"))
                if all(hardened.get(d) is not None for d in ("userspace", "v8", "kernel")):
                    total = sum(_number(hardened[d], integer=True) for d in ("userspace", "v8", "kernel"))
                    extra = {**base["conditions"], "calculation": "Sum of three explicitly reported mitigated domain counts"}
                    out.append(_finish({**base, "conditions": extra}, "intended_vulnerability_exploits", total, unit="count", mitigations="Enabled; domain-specific defenses"))
            if not found:
                raise BenchmarkParseError("ExploitGym row has no recognized outcome")
    return out


def _csv_number(value: str, *, count=False) -> float | int | None:
    if value is None or not value.strip() or value.strip() in ("-", "—", "N/A"):
        return None
    try:
        return _number(float(value.strip().rstrip("%")), integer=count, percent=not count)
    except (TypeError, ValueError) as exc:
        raise BenchmarkParseError("Invalid numeric table value") from exc


def _cybench(raw: str, url: str, sha: str, checked: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(raw))
    expected = {"Model", "Count", "End-to-End % Solved", "Subtask-Guided % Solved", "Subtasks % Solved"}
    if not reader.fieldnames or not expected <= set(reader.fieldnames):
        raise BenchmarkParseError("Cybench CSV columns changed")
    out = []
    for row in reader:
        label = row.get("Model", "")
        model = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰†‡]+", "", label).strip()
        base = _base("cybench", {"model": model, "source": "Cybench official leaderboard (mixed evaluators)"}, url, sha, checked)
        if base is None:
            continue
        total = _csv_number(row.get("Count"), count=True)
        if not total:
            raise BenchmarkParseError("Cybench task count missing or zero")
        base.update(total_tasks=total, dataset_subset=f"Reported {total}-task subset; task IDs not included in CSV")
        base["conditions"].update({"leaderboard_label": label, "footnote_markers": "".join(re.findall(r"[¹²³⁴⁵⁶⁷⁸⁹⁰†‡]", label)), "source_flag_success_count": row.get("Flag Success Count"), "human_first_solve_time_unguided": row.get("FST Standard"), "human_first_solve_time_guided": row.get("FST Subtask")})
        base["notes"] = "Mixed model/system-card evaluations and task subsets. Attempt policy, harness, and date require the original report. Superscript footnotes are retained in conditions. Published corrected rate takes precedence over a stale raw count; neither is silently recomputed."
        for column, metric, setting in (("End-to-End % Solved", "unguided_success", "Unguided"), ("Subtask-Guided % Solved", "guided_success", "Subtask-guided"), ("Subtasks % Solved", "subtask_success_macro_average", "Macro-average over subtasks")):
            value = _csv_number(row.get(column))
            if value is not None:
                out.append(_finish(base, metric, value, setting=setting))
    return out


class _ResultTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active = False
        self.depth = 0
        self.rows = []
        self.row = []
        self.cell = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table" and (self.active or attrs.get("id") == "results-table"):
            self.depth += 1
            self.active = True
        if self.active:
            if tag == "tr":
                self.row = []
            if tag in ("th", "td"):
                self.cell = []
            if tag == "a" and attrs.get("href"):
                self.links.append(attrs["href"])

    def handle_data(self, data):
        if self.active and self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if not self.active:
            return
        if tag in ("th", "td") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        if tag == "tr":
            self.rows.append((self.row, self.links))
            self.links = []
        if tag == "table":
            self.depth -= 1
            self.active = self.depth > 0


def _agentdojo(raw: str, url: str, sha: str, checked: str) -> list[dict]:
    parser = _ResultTable()
    parser.feed(raw)
    expected = ["Provider", "Model", "Defense", "Attack", "Utility", "Utility under attack", "Targeted ASR", "Date", "Traces"]
    if not parser.rows or parser.rows[0][0] != expected:
        raise BenchmarkParseError("AgentDojo result table columns changed or table missing")
    out = []
    for cells, links in parser.rows[1:]:
        if len(cells) != len(expected):
            raise BenchmarkParseError("AgentDojo result row length changed")
        row = dict(zip(expected, cells))
        base = _base("agentdojo", {"model": row["Model"], "date": row["Date"], "source": "AgentDojo official published results", "agent": "AgentDojo pipeline"}, url, sha, checked)
        if base is None:
            continue
        if row["Provider"].lower() != base["provider"].lower():
            raise BenchmarkParseError("AgentDojo model/provider mismatch")
        base.update(model_id=row["Model"], setting=f"Attack: {row['Attack']}; defense: {row['Defense']}", safeguards=row["Defense"])
        base["conditions"].update({"attack": row["Attack"], "defense": row["Defense"], "trace_urls": links})
        base["notes"] = "The authors explicitly state these results are not a fair leaderboard: attack and defense combinations differ. Interpret attack success alongside utility. Exact task counts and retry budgets are not in this result table."
        for column, metric, direction in (("Utility", "benign_utility", "higher_is_better"), ("Utility under attack", "utility_under_attack", "higher_is_better"), ("Targeted ASR", "targeted_attack_success", "lower_is_better")):
            value = _csv_number(row[column])
            if value is None:
                raise BenchmarkParseError("AgentDojo required metric missing")
            out.append(_finish(base, metric, value, metric_direction=direction))
    return out


def _cve_yaml(raw: str, url: str, sha: str, checked: str) -> list[dict]:
    try:
        row = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise BenchmarkParseError("Invalid CVE-Bench metadata YAML") from exc
    if not isinstance(row, dict) or not isinstance(row.get("variations"), dict) or not row.get("benchmarkVersion"):
        raise BenchmarkParseError("CVE-Bench metadata schema changed")
    base = _base("cve_bench", row, url, sha, checked)
    if base is None:
        return []
    if str(row.get("modelOrg", "")).lower() != base["provider"].lower():
        raise BenchmarkParseError("CVE-Bench model/provider mismatch")
    base.update(model_id=row["model"], benchmark_version=str(row["benchmarkVersion"]),
                evaluation_date=base["published_date"], published_date=None, attempts=1,
                dataset_subset="Standard published variant; per-run task count not present in metadata")
    base["conditions"].update({"reported_date_semantics": "Run date per official submission schema", "agent_url": _text(row.get("agentUrl")), "benchmark_version_url": _text(row.get("benchmarkVersionUrl")), "label": _text(row.get("label"))})
    base["notes"] = "The zero-day setting withholds the vulnerability description for historical CVEs; it is not a count of previously unpublished zero-days. Dataset and scoring versions must match before comparison."
    out = []
    for variant, value in row["variations"].items():
        if variant not in ("zeroDay", "oneDay") or not isinstance(value, dict) or "passAt1" not in value:
            raise BenchmarkParseError("Unsupported CVE-Bench variant or missing passAt1")
        average_cost = None if value.get("avgCostPerTask") is None else _number(value["avgCostPerTask"])
        conditions = {**base["conditions"], "variant_key": variant, "trajectory_url": _text(value.get("trajUrl")), "average_cost_per_task_usd": average_cost}
        setting = "Zero-day setting; vulnerability description withheld" if variant == "zeroDay" else "One-day setting; vulnerability description provided"
        out.append(_finish({**base, "conditions": conditions}, "pass_at_1", value["passAt1"], setting=setting))
    return out


def parse_benchmark_source(benchmark_id: str, raw: bytes | str, source_url: str, *, retrieved_at: str | None = None, allow_empty=False) -> list[dict]:
    """Parse one official data representation, retaining its exact byte hash."""
    if benchmark_id not in CORE_BENCHMARKS:
        raise BenchmarkParseError("Unknown core benchmark")
    payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    sha = hashlib.sha256(payload).hexdigest()
    checked = retrieved_at or _now()
    try:
        text = payload.decode("utf-8-sig")
        if benchmark_id == "cybench":
            observations = _cybench(text, source_url, sha, checked)
        elif benchmark_id == "agentdojo":
            observations = _agentdojo(text, source_url, sha, checked)
        elif benchmark_id == "cve_bench":
            observations = _cve_yaml(text, source_url, sha, checked)
        else:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise BenchmarkParseError("Expected a JSON object")
            observations = _observatory(benchmark_id, data, source_url, sha, checked)
    except (UnicodeError, json.JSONDecodeError, TypeError, KeyError, OverflowError) as exc:
        raise BenchmarkParseError("Published source format could not be parsed") from exc
    if not observations and not allow_empty:
        raise BenchmarkParseError("No in-scope published observations found; do not treat as an empty leaderboard")
    ids = [o["source_result_id"] for o in observations]
    if len(ids) != len(set(ids)):
        raise BenchmarkParseError("Duplicate result identities; source requires review")
    return observations


def collect_benchmarks(settings: dict, evidence_dir: Path, *, session=None, resolver=None, timeout=30, max_bytes=10_000_000) -> dict:
    """Fetch configured core sources and return observations plus source status.

    CVE-Bench is all-or-nothing per refresh: a truncated index, changed Git blob,
    or unavailable run prevents acceptance of a partial catalog for that source.
    Other independent benchmarks can still succeed. Existing register findings
    are never deleted here. The caller owns merge/review and writer locking.
    """
    result = {"observations": [], "source_checks": [], "errors": []}
    if not settings.get("enabled", True):
        return result
    selected = settings.get("core", list(CORE_BENCHMARKS))
    if not isinstance(selected, list) or not all(isinstance(x, str) for x in selected) or len(set(selected)) != len(selected) or set(selected) - set(CORE_BENCHMARKS):
        raise ValueError("benchmarks.core must contain unique supported benchmark IDs")
    overrides = settings.get("sources", {})
    if not isinstance(overrides, dict) or set(overrides) - set(CORE_BENCHMARKS):
        raise ValueError("benchmarks.sources must map supported benchmark IDs to public data URLs")
    allowed_hosts = settings.get("allowed_hosts", DEFAULT_ALLOWED_HOSTS)
    if not isinstance(allowed_hosts, (list, tuple)) or not allowed_hosts:
        raise ValueError("benchmarks.allowed_hosts must be a nonempty exact-host list")
    max_runs = settings.get("max_cve_runs", 200)
    if not isinstance(max_runs, int) or isinstance(max_runs, bool) or not 1 <= max_runs <= 1000:
        raise ValueError("max_cve_runs must be between 1 and 1000")
    directory = Path(evidence_dir)

    def fetch(benchmark, url):
        sid = "bmk_" + benchmark + "_" + hashlib.sha256(url.encode()).hexdigest()[:12]
        try:
            check = fetch_sources([{"source_id": sid, "url": url}], directory, allowed_hosts,
                                  session=session, resolver=resolver, timeout=timeout, max_bytes=max_bytes)[0]
        except (CollectionError, ImportError, OSError, ValueError) as exc:
            check = {"source_id": sid, "url": url, "status": "unavailable", "checked_at": _now(), "error": "Benchmark fetch failed (" + type(exc).__name__ + ")"}
        check["benchmark_id"] = benchmark
        result["source_checks"].append(check)
        if check["status"] == "unavailable":
            raise BenchmarkParseError(check.get("error", "Source unavailable"))
        return check, Path(check["raw_path"]).read_bytes()

    for benchmark in selected:
        check = None
        try:
            url = overrides.get(benchmark, DEFAULT_SOURCES[benchmark])
            if not isinstance(url, str):
                raise BenchmarkParseError("Source URL must be a string")
            check, raw = fetch(benchmark, url)
            if benchmark != "cve_bench":
                observations = parse_benchmark_source(benchmark, raw, url, retrieved_at=check["checked_at"])
            else:
                tree = json.loads(raw)
                if not isinstance(tree, dict) or tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
                    raise BenchmarkParseError("CVE-Bench index missing or truncated")
                runs = [x for x in tree["tree"] if isinstance(x, dict) and re.fullmatch(r"runs/[A-Za-z0-9_.-]+/metadata\.ya?ml", str(x.get("path", "")))]
                if not runs or len(runs) > max_runs:
                    raise BenchmarkParseError("CVE-Bench run index is empty or exceeds configured limit")
                observations = []
                for entry in runs:
                    if entry.get("type") != "blob" or not re.fullmatch(r"[0-9a-f]{40}", str(entry.get("sha", ""))):
                        raise BenchmarkParseError("CVE-Bench index entry is not a Git blob")
                    run_url = "https://raw.githubusercontent.com/uiuc-kang-lab/cvebench.com/master/" + entry["path"]
                    run_check, body = fetch(benchmark, run_url)
                    # Detect branch changes between tree and raw fetch. Never
                    # accept a mismatched run as if it belonged to this index.
                    blob_hash = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
                    if blob_hash != entry["sha"]:
                        raise BenchmarkParseError("CVE-Bench Git blob changed during collection; retry later")
                    parsed = parse_benchmark_source(benchmark, body, run_url, retrieved_at=run_check["checked_at"], allow_empty=True)
                    for observation in parsed:
                        observation["source_revision"] = entry["sha"]
                        observation["conditions"].update({"index_sha256": check["raw_sha256"], "git_blob_sha1": entry["sha"]})
                    observations.extend(parsed)
                    run_check["parsed_observations"] = len(parsed)
                if not observations:
                    raise BenchmarkParseError("No in-scope CVE-Bench results found")
            check["parsed_observations"] = len(observations)
            check["parse_status"] = "parsed"
            check["collection_scope"] = (
                "Public primary Level 1 single-trial rows only; hidden rows, other levels, multi-trial rows, third-party models and multi-model ensembles excluded"
                if benchmark == "cybergym" else
                "Published single-model observations for Anthropic, OpenAI and Google; hidden rows and multi-model ensembles excluded"
            )
            result["observations"].extend(observations)
        except (BenchmarkParseError, ValueError, OSError) as exc:
            error = str(exc) if isinstance(exc, BenchmarkParseError) else "Source parse failed (" + type(exc).__name__ + ")"
            result["errors"].append({"benchmark_id": benchmark, "error": error})
            if check is not None:
                check["fetch_status"] = check["status"]
                check.update(status="unavailable", parse_status="failed", error=error)
    return result
