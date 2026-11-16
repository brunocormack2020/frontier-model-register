"""Supplementary monitoring must preserve evidence and expose source changes."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from frontier_register import benchmarks as b
from frontier_register.benchmark_monitoring import refresh_supplementary_sources, ALLOWED_HOSTS
from test_benchmark_workflow import fixture_state, observation, sole_entry, accept_pending


def aisi_state():
    state = fixture_state()
    finding = observation(source_url="https://www.aisi.gov.uk/blog/test-range", scenario="Fixture range",
                          metric="mean_steps", value=9.8, unit="steps", successes=None,
                          total_tasks=32, source_result_id="range-10m", active_defenders=False)
    b.merge_observations(state, [finding], kind="aisi")
    return accept_pending(state)


def fetched(hash_value, *, status="new"):
    def run(sources, *_args, **_kwargs):
        return [{**source, "status": status, "raw_sha256": hash_value,
                 "raw_path": "/evidence/document.pdf", "text_path": "/evidence/document.txt",
                 "checked_at": "2026-11-14T12:00:00Z"} for source in sources]
    return run


class SupplementarySourceTests(unittest.TestCase):
    def test_source_change_preserves_values_review_and_original_hash(self):
        state = aisi_state()
        original = copy.deepcopy(sole_entry(state, "aisi"))
        organization = copy.deepcopy(state["records"][0]["organization"])
        with tempfile.TemporaryDirectory() as tmp, patch("frontier_register.benchmark_monitoring.fetch_sources", side_effect=fetched("b" * 64)):
            result = refresh_supplementary_sources(state, Path(tmp))
            entry = sole_entry(state, "aisi")
            self.assertEqual(1, result["changed_observations"])
            self.assertEqual(original["data"], entry["data"])
            self.assertEqual(original["review"], entry["review"])
            self.assertEqual(original["content_sha256"], entry["content_sha256"])
            self.assertEqual(organization, state["records"][0]["organization"])
            self.assertTrue(entry["source_review_required"])
            self.assertEqual("changed", entry["source_watch"]["status"])
            self.assertEqual("b" * 64, entry["source_watch"]["raw_sha256"])
            again = refresh_supplementary_sources(state, Path(tmp))
            self.assertEqual(0, again["changed_observations"])
        # A source reverting to baseline still needs the outstanding review.
        with tempfile.TemporaryDirectory() as tmp, patch("frontier_register.benchmark_monitoring.fetch_sources", side_effect=fetched("a" * 64)):
            refresh_supplementary_sources(state, Path(tmp))
        self.assertTrue(sole_entry(state, "aisi")["source_review_required"])

    def test_latest_review_hash_is_preferred_and_missing_baseline_is_explicit(self):
        state = aisi_state()
        entry = sole_entry(state, "aisi")
        entry["review"]["source_sha256"] = "b" * 64
        with tempfile.TemporaryDirectory() as tmp, patch("frontier_register.benchmark_monitoring.fetch_sources", side_effect=fetched("b" * 64)):
            result = refresh_supplementary_sources(state, Path(tmp))
        self.assertEqual(0, result["changed_observations"])
        self.assertFalse(sole_entry(state, "aisi").get("source_review_required", False))
        self.assertEqual("unchanged", sole_entry(state, "aisi")["source_watch"]["status"])
        entry = sole_entry(state, "aisi")
        entry["review"].pop("source_sha256")
        entry["data"]["source_sha256"] = None
        with tempfile.TemporaryDirectory() as tmp, patch("frontier_register.benchmark_monitoring.fetch_sources", side_effect=fetched("c" * 64)):
            result = refresh_supplementary_sources(state, Path(tmp))
        self.assertEqual(0, result["changed_observations"])
        self.assertTrue(sole_entry(state, "aisi")["source_review_required"])
        self.assertEqual("baseline_missing", sole_entry(state, "aisi")["source_watch"]["status"])

    def test_proxy_dependency_failure_retains_findings_and_later_success_clears_error(self):
        state = aisi_state()
        original = copy.deepcopy(sole_entry(state, "aisi"))
        with tempfile.TemporaryDirectory() as tmp, patch("frontier_register.benchmark_monitoring.fetch_sources", side_effect=ImportError("socksio package is not installed")):
            report = refresh_supplementary_sources(state, Path(tmp))
        self.assertEqual(0, report["changed_observations"])
        self.assertIn("socksio", report["errors"][0]["error"])
        entry = sole_entry(state, "aisi")
        self.assertEqual(original["data"], entry["data"])
        self.assertEqual(original["review"], entry["review"])
        self.assertEqual("unavailable", entry["source_watch"]["status"])
        self.assertIn("source unavailable", b.monitoring(state["records"][0], state).lower())
        with tempfile.TemporaryDirectory() as tmp, patch("frontier_register.benchmark_monitoring.fetch_sources", side_effect=fetched("a" * 64)):
            report = refresh_supplementary_sources(state, Path(tmp))
        self.assertEqual([], report["errors"])
        self.assertFalse(any(c.get("status") == "unavailable" for c in state["benchmarks"]["source_checks"].values()))

    def test_scope_selection_and_shared_source_are_handled_separately(self):
        state = aisi_state()
        common = "https://meta-llama.github.io/PurpleLlama/secure-code"
        b.merge_observations(state, [observation(benchmark_id="secure_code_instruct", source_url=common),
                                    observation(benchmark_id="secure_code_autocomplete", source_url=common),
                                    observation(benchmark_id="cybersoceval_malware", source_url="https://meta-llama.github.io/PurpleLlama/malware")])
        b.set_scope(state, coding=True, soc=False, actor="Scope owner")
        with tempfile.TemporaryDirectory() as tmp, patch("frontier_register.benchmark_monitoring.fetch_sources", side_effect=fetched("a" * 64)) as fetch:
            result = refresh_supplementary_sources(state, Path(tmp), timeout=12, max_bytes=12345)
        self.assertEqual(1, fetch.call_count)
        self.assertEqual(2, len(fetch.call_args.args[0]))
        self.assertEqual(ALLOWED_HOSTS, fetch.call_args.args[2])
        self.assertEqual(12, fetch.call_args.kwargs["timeout"])
        self.assertEqual({"aisi_cyber_range", "secure_code_instruct", "secure_code_autocomplete"},
                         {c["benchmark_id"] for c in result["source_checks"]})
        soc = next(e for _, e in b.iter_entries(state) if e["data"]["benchmark_id"] == "cybersoceval_malware")
        self.assertNotIn("source_watch", soc)

    def test_no_selected_sources_has_no_network_or_state_effect(self):
        state = fixture_state()
        b.merge_observations(state, [observation(), observation(benchmark_id="secure_code_instruct")])
        before = copy.deepcopy(state)
        with tempfile.TemporaryDirectory() as tmp, patch("frontier_register.benchmark_monitoring.fetch_sources") as fetch:
            report = refresh_supplementary_sources(state, Path(tmp))
        fetch.assert_not_called()
        self.assertEqual(before, state)
        self.assertEqual({"source_checks": [], "errors": [], "changed_observations": 0}, report)

    def test_unapproved_host_does_not_send_request(self):
        state = aisi_state()
        sole_entry(state, "aisi")["data"]["source_url"] = "https://unapproved.example.test/report"
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=b"Should not be requested")
        with tempfile.TemporaryDirectory() as tmp, httpx.Client(transport=httpx.MockTransport(handler)) as session:
            report = refresh_supplementary_sources(state, Path(tmp), session=session, resolver=lambda _host: None)
        self.assertFalse(requests)
        self.assertEqual(1, len(report["errors"]))
        self.assertEqual("unavailable", sole_entry(state, "aisi")["source_watch"]["status"])


if __name__ == "__main__":
    unittest.main()
