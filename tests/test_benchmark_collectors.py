"""Meaningful data-integrity and failure-boundary tests for public score import."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from frontier_register.benchmark_collectors import (
    BenchmarkParseError, DEFAULT_SOURCES, collect_benchmarks, parse_benchmark_source,
)


WHEN = "2026-11-14T17:00:00Z"


def parse(benchmark, data):
    raw = json.dumps(data) if isinstance(data, dict) else data
    return parse_benchmark_source(benchmark, raw, DEFAULT_SOURCES[benchmark], retrieved_at=WHEN)


def cg(**updates):
    return {"model": "GPT-5", "agent": "Example scaffold", "date": "2026-01-01", "trials": 1, "score_10": 0.25, **updates}


def dojo_table(rows):
    headers = ["Provider", "Model", "Defense", "Attack", "Utility", "Utility under attack", "Targeted ASR", "Date", "Traces"]
    return '<table id="results-table"><tr>' + ''.join('<th>'+h+'</th>' for h in headers) + '</tr>' + ''.join('<tr>'+''.join('<td>'+c+'</td>' for c in row)+'</tr>' for row in rows) + '</table>'


class ParserTests(unittest.TestCase):
    def test_cybergym_primary_fraction_and_snapshot_hash(self):
        data = {"level1": [cg()]}
        observation = parse("cybergym", data)[0]
        self.assertEqual(observation["value"], 25)
        self.assertEqual(observation["attempts"], 1)
        self.assertIsNone(observation["model_id"])
        self.assertIsNone(observation["total_tasks"])
        self.assertEqual(observation["source_sha256"], hashlib.sha256(json.dumps(data).encode()).hexdigest())

    def test_cybergym_does_not_infer_vendor_from_claude_code(self):
        rows = parse("cybergym", {"level1": [cg(), cg(model="Qwen3", agent="Claude Code")]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "OpenAI")

    def test_hidden_rows_and_ensembles_are_not_published_scores(self):
        rows = parse("cybergym", {"level1": [cg(), cg(model="Claude Opus 5", hidden=True), cg(model="Multi-model (GPT-5, Claude Opus 5)")]})
        self.assertEqual(len(rows), 1)

    def test_unsupported_fraction_nonfinite_and_negative_rejected(self):
        for value in (1.1, -0.2, float("nan"), float("inf"), True, "0.5"):
            with self.subTest(value=value), self.assertRaises(BenchmarkParseError):
                parse("cybergym", {"level1": [cg(score_10=value)]})

    def test_correction_stable_identity_and_distinct_features(self):
        first = parse("cybergym", {"level1": [cg()]})[0]
        changed = parse("cybergym", {"level1": [cg(score_10=0.4)]})[0]
        other = parse("cybergym", {"level1": [cg(features=["test-time mem."])]})[0]
        self.assertEqual(first["source_result_id"], changed["source_result_id"])
        self.assertNotEqual(first["source_sha256"], changed["source_sha256"])
        self.assertNotEqual(first["source_result_id"], other["source_result_id"])

    def test_unexpected_empty_source_is_an_error(self):
        for data in ({}, {"level1": []}, {"level1": [cg(trials=3)]}):
            with self.subTest(data=data), self.assertRaises(BenchmarkParseError):
                parse("cybergym", data)

    def test_e2e_preserves_each_stage_budget_and_denominator(self):
        rows = parse("cybergym_e2e", {"results": [{"model": "Claude Opus 4.6", "harness": "Claude Code", "task_set": 615, "budget": "$10 / 90 min", "patch_only": 80, "s1": 50, "s2": 45, "s3": 40, "s4": 20}]})
        self.assertEqual(len(rows), 5)
        self.assertEqual({r["total_tasks"] for r in rows}, {615})
        self.assertEqual(rows[0]["budget"], {"reported_limit": "$10 / 90 min"})
        self.assertNotEqual(rows[0]["setting"], rows[1]["setting"])

    def test_e2e_new_budget_is_distinct_observation(self):
        common = {"model": "Claude Opus 4.6", "task_set": 920, "s3": 60}
        a = parse("cybergym_e2e", {"results": [{**common, "budget": "$10"}]})[0]
        b = parse("cybergym_e2e", {"results": [{**common, "budget": "Uncapped"}]})[0]
        self.assertNotEqual(a["source_result_id"], b["source_result_id"])

    def test_exploit_counts_are_not_divided_by_full_benchmark(self):
        rows = parse("exploitgym", {"instances": {"total": 869}, "results": [{"model": "GPT-5.5", "on_target": 15, "flag_captured": 20, "eval_note": "2h"}]})
        self.assertEqual({r["unit"] for r in rows}, {"count"})
        self.assertEqual([r["value"] for r in rows], [15, 20])
        self.assertTrue(all(r["total_tasks"] is None for r in rows))

    def test_exploit_missing_domains_remain_missing(self):
        rows = parse("exploitgym", {"results": [{"model": "Gemini 3.1 Pro", "on_target": 0}]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 0)
        self.assertEqual(rows[0]["metric"], "intended_vulnerability_exploits")

    def test_child_variant_does_not_inherit_parent_measurements(self):
        rows = parse("exploitgym", {"results": [{"model": "GPT-5.5", "eval_note": "6h", "on_target": 20, "flag_captured": 25, "userspace": 10, "v8": 5, "kernel": 5, "subRows": [{"eval_note": "2h", "on_target": 8}]}]})
        child = [r for r in rows if r["setting"] == "2h"]
        self.assertEqual(len(child), 1)
        self.assertEqual(child[0]["value"], 8)

    def test_exploit_subrows_and_mitigations_are_separate(self):
        rows = parse("exploitgym", {"results": [{"model": "GPT-5.5", "on_target": 20, "eval_note": "6h", "mitigated": {"userspace": 1, "v8": 2, "kernel": 3}, "subRows": [{"on_target": 10, "eval_note": "2h", "mitigated": {"userspace": 0, "v8": 0, "kernel": 0}}]}]})
        totals = [r for r in rows if r["metric"] == "intended_vulnerability_exploits"]
        self.assertEqual(sorted(r["value"] for r in totals), [0, 6, 10, 20])
        self.assertEqual(len({r["source_result_id"] for r in rows}), len(rows))

    def test_cyberbin_preserves_real_zero_and_month_date(self):
        rows = parse("cyberbingym", {"instances": {"total": 100}, "results": [{"model": "Claude Opus 5", "date": "2026-10", "poc": 80, "exploit": 0}]})
        self.assertEqual([r["value"] for r in rows], [80, 0])
        self.assertEqual(rows[0]["published_date"], "2026-10")
        self.assertIsNone(rows[0]["evaluation_date"])

    def test_cybench_retains_corrected_rate_not_stale_numerator(self):
        csv = 'Model,Count,End-to-End % Solved,Flag Success Count,Subtask-Guided % Solved,Subtasks % Solved\nOpenAI o3-mini ¹†,40,22.5,10,,\n'
        row = parse("cybench", csv)[0]
        self.assertEqual(row["value"], 22.5)
        self.assertIsNone(row["successes"])
        self.assertEqual(row["conditions"]["source_flag_success_count"], "10")
        self.assertEqual(row["conditions"]["footnote_markers"], "¹†")

    def test_agentdojo_different_attacks_stay_distinct(self):
        rows = [["openai", "gpt-4o-2024-05-13", "None", attack, "70%", "60%", "5%", "2025-01-01", ""] for attack in ("attack_a", "attack_b")]
        observations = parse("agentdojo", dojo_table(rows))
        self.assertEqual(len(observations), 6)
        self.assertEqual(len({r["source_result_id"] for r in observations}), 6)
        self.assertEqual([r["metric_direction"] for r in observations[:3]], ["higher_is_better", "higher_is_better", "lower_is_better"])
        self.assertEqual(observations[0]["model_id"], "gpt-4o-2024-05-13")

    def test_agentdojo_schema_drift_fails_closed(self):
        with self.assertRaises(BenchmarkParseError):
            parse("agentdojo", '<html><table><tr><td>GPT-5</td><td>90%</td></tr></table></html>')

    def test_cve_metadata_preserves_variants_and_api_identity(self):
        raw = 'model: claude-opus-4-6\nmodelOrg: Anthropic\nagent: example\ndate: "2026-04-11"\nbenchmarkVersion: v2.1.0\nvariations:\n  zeroDay:\n    passAt1: 32.5\n  oneDay:\n    passAt1: 40\n'
        rows = parse("cve_bench", raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["model_id"], "claude-opus-4-6")
        self.assertEqual(rows[0]["evaluation_date"], "2026-04-11")
        self.assertIsNone(rows[0]["published_date"])
        self.assertTrue(all(r["total_tasks"] is None for r in rows))


class CollectionTests(unittest.TestCase):
    def run_collect(self, directory, responses, core, **settings):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            value = responses.get(str(request.url), (404, b"missing"))
            status, body = value
            return httpx.Response(status, content=body, headers={"content-type": "text/plain"})
        with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
            result = collect_benchmarks({"enabled": True, "core": core, **settings}, Path(directory), session=client, resolver=lambda host: None)
        return result, calls

    def test_collection_snapshots_success_and_retains_prior_on_failure(self):
        url = DEFAULT_SOURCES["cybergym"]
        raw = json.dumps({"level1": [cg()]}).encode()
        with tempfile.TemporaryDirectory() as directory:
            first, _ = self.run_collect(directory, {url: (200, raw)}, ["cybergym"])
            second, _ = self.run_collect(directory, {url: (503, b"outage")}, ["cybergym"])
            self.assertEqual(len(first["observations"]), 1)
            self.assertTrue(Path(first["source_checks"][0]["raw_path"]).is_file())
            self.assertEqual(second["observations"], [])
            self.assertEqual(second["source_checks"][0]["status"], "unavailable")
            self.assertEqual(second["source_checks"][0]["raw_sha256"], first["source_checks"][0]["raw_sha256"])

    def test_parser_error_is_not_an_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.run_collect(directory, {DEFAULT_SOURCES["cybergym"]: (200, b'{"wrong":[]}')}, ["cybergym"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["source_checks"][0]["parse_status"], "failed")

    def test_unapproved_source_override_does_not_send_request(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.run_collect(directory, {}, ["cybergym"], sources={"cybergym": "https://evil.example/data.json"})
        self.assertEqual(calls, [])
        self.assertEqual(len(result["errors"]), 1)

    def test_cve_index_and_blob_integrity(self):
        body = b'model: gpt-4o-2024-11-20\nmodelOrg: OpenAI\nbenchmarkVersion: v1.0\nvariations:\n  oneDay:\n    passAt1: 7\n'
        path = 'runs/20250321_example/metadata.yml'
        blob = hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()
        index = json.dumps({"truncated": False, "tree": [{"path": path, "type": "blob", "sha": blob}]}).encode()
        run_url = 'https://raw.githubusercontent.com/uiuc-kang-lab/cvebench.com/master/'+path
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.run_collect(directory, {DEFAULT_SOURCES['cve_bench']: (200,index), run_url: (200,body)}, ['cve_bench'])
        self.assertEqual(len(result["observations"]), 1)
        self.assertEqual(result["observations"][0]["source_revision"], blob)
        self.assertEqual(len(calls), 2)

    def test_cve_changed_blob_rejects_partial_collection(self):
        path = 'runs/example/metadata.yml'
        index = json.dumps({"truncated": False, "tree": [{"path": path, "type": "blob", "sha": '0'*40}]}).encode()
        run_url = 'https://raw.githubusercontent.com/uiuc-kang-lab/cvebench.com/master/'+path
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.run_collect(directory, {DEFAULT_SOURCES['cve_bench']: (200,index), run_url: (200,b'changed')}, ['cve_bench'])
        self.assertEqual(result["observations"], [])
        self.assertIn('Git blob changed', result["errors"][0]["error"])

    def test_cve_truncated_index_and_unsafe_paths_fail_closed(self):
        for index in ({"truncated": True, "tree": []}, {"truncated": False, "tree": [{"path": "runs/../../secret", "type": "blob", "sha": "0"*40}]}):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                result, calls = self.run_collect(directory, {DEFAULT_SOURCES['cve_bench']: (200,json.dumps(index).encode())}, ['cve_bench'])
                self.assertEqual(result["observations"], [])
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(result["errors"]), 1)

    def test_disabled_does_not_open_network(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(collect_benchmarks({"enabled": False}, Path(directory)), {"observations": [], "source_checks": [], "errors": []})


if __name__ == "__main__":
    unittest.main()
