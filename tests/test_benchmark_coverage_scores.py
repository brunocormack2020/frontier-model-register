"""Score-view regressions: truthful matching, retained context and workbook links."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_benchmark_workflow import fixture_state, observation, accept_pending, sole_entry
from frontier_register import benchmarks as b
from frontier_register.benchmark_views import coverage_preview, workbook_tables
from frontier_register.core import digest, import_workbook


class CoverageScorePreviews(unittest.TestCase):
    def preview(self, state, bid="cybergym"):
        return coverage_preview(state, state["records"][0], bid)

    def test_exact_pending_zero_is_visible_with_metric_configuration_and_review(self):
        state = fixture_state()
        b.merge_observations(state, [observation(value=0, successes=0)])
        before = copy.deepcopy(state)
        result = self.preview(state)
        for expected in ("0%", "success_rate", "Exact ID match", "Pending review",
                         "Gemini Test 001", "level_1", "Fixture agent"):
            self.assertIn(expected, result["text"])
        self.assertEqual("'Benchmark Results'!A5", result["location"])
        self.assertEqual(before, state)

    def test_candidate_score_never_becomes_a_mapping_or_organization_evidence(self):
        state = fixture_state()
        b.merge_observations(state, [observation(model_id=None)])
        before = digest(state)
        evidence = b.evidence_digest(state["records"][0], state)
        result = self.preview(state)
        self.assertIn("40%", result["text"])
        self.assertIn("Unconfirmed name/version match", result["text"])
        self.assertIn("Pending review", result["text"])
        self.assertIsNone(sole_entry(state)["record_id"])
        self.assertEqual(before, digest(state))
        self.assertEqual(evidence, b.evidence_digest(state["records"][0], state))

    def test_disclosed_conflicting_id_is_not_shown_as_an_unconfirmed_score(self):
        state = fixture_state()
        b.merge_observations(state, [observation(model_id="gemini-test-mini-001")])
        result = self.preview(state)
        self.assertEqual("No linked published result", result["text"])
        self.assertIsNone(result["location"])

    def test_reviewed_label_keeps_snapshot_uncertainty(self):
        state = fixture_state()
        b.merge_observations(state, [observation(model_id=None)])
        state = accept_pending(state, record_id=state["records"][0]["record_id"],
                               mapping_basis="Reviewed the matching label; the underlying snapshot is not disclosed.")
        result = self.preview(state)
        self.assertIn("40%", result["text"])
        self.assertIn("Reviewed label; snapshot unconfirmed", result["text"])
        self.assertIn("Accepted evidence", result["text"])

    def test_rejected_values_stay_in_details_but_out_of_coverage(self):
        state = fixture_state()
        b.merge_observations(state, [observation()])
        queue = b.make_benchmark_review_queue(state)
        queue["items"][0].update(action="reject", review_note="Excluded after checking the test configuration.")
        state = b.apply_benchmark_review(state, queue, "Reviewer")
        result = self.preview(state)
        self.assertNotIn("40%", result["text"])
        self.assertIsNone(result["location"])
        tables = workbook_tables(state)
        details = next(t for t in tables if t["sheet"] == "Benchmark Results")
        self.assertEqual(0.4, details["rows"][0][4])
        self.assertEqual("rejected", details["rows"][0][12])

    def test_units_metrics_and_configurations_remain_separate(self):
        state = fixture_state()
        b.merge_observations(state, [
            observation(metric="alpha_rate", value=10, successes=1, setting="one-hour budget"),
            observation(metric="beta_fraction", value=0.5, unit="fraction", successes=None,
                        setting="two-hour budget", source_result_id="second"),
            observation(metric="gamma_count", value=7, unit="count", successes=None,
                        total_tasks=None, setting="three-hour budget", source_result_id="third"),
        ])
        result = self.preview(state)
        for expected in ("10%", "0.5 (fraction)", "7 count", "alpha_rate", "beta_fraction",
                         "gamma_count", "one-hour budget", "two-hour budget", "three-hour budget"):
            self.assertIn(expected, result["text"])
        self.assertEqual(3, len(result["observation_ids"]))

    def test_preview_is_bounded_and_discloses_more_results(self):
        state = fixture_state()
        b.merge_observations(state, [observation(source_result_id=f"run-{n}", value=10*n, successes=n)
                                     for n in range(1, 6)])
        result = self.preview(state)
        self.assertEqual(3, len(result["observation_ids"]))
        self.assertIn("Showing 3 of 5; see details", result["text"])
        self.assertIn("10%", result["text"])
        self.assertNotIn("50%", result["text"])
        # Source insertion order cannot change the selected preview rows.
        state["benchmarks"]["observations"] = dict(reversed(list(state["benchmarks"]["observations"].items())))
        self.assertEqual(result, self.preview(state))

    def test_linked_and_candidate_results_both_remain_visible(self):
        state = fixture_state()
        b.merge_observations(state, [observation(source_result_id=f"exact-{n}", value=10*n, successes=n)
                                     for n in range(1, 4)] + [
            observation(model_id=None, source_result_id="candidate", value=80, successes=8)])
        result = self.preview(state)
        self.assertIn("3 linked, 1 unconfirmed observations", result["text"])
        self.assertIn("Exact ID match", result["text"])
        self.assertIn("Unconfirmed name/version match", result["text"])
        self.assertIn("80%", result["text"])
        self.assertIn("Showing 3 of 4; see details", result["text"])
        self.assertEqual(3, len(result["observation_ids"]))

    def test_scope_off_keeps_reference_score_without_changing_selected_scope(self):
        state = fixture_state()
        b.merge_observations(state, [observation(benchmark_id="secure_code_instruct",
                                                metric="vulnerable_percentage")])
        state = accept_pending(state)
        before, evidence = digest(state), b.evidence_digest(state["records"][0], state)
        result = self.preview(state, "secure_code_instruct")
        self.assertIn("Scope off; reference only", result["text"])
        self.assertIn("40%", result["text"])
        self.assertIn("vulnerable_percentage", result["text"])
        self.assertFalse(state["benchmarks"]["scope"]["coding"])
        self.assertEqual(before, digest(state))
        self.assertEqual(evidence, b.evidence_digest(state["records"][0], state))

    def test_aisi_links_to_its_own_scenario_details_without_aggregate_score(self):
        state = fixture_state()
        finding = observation(scenario="Corporate range", metric="mean_steps_completed", value=9.8,
                              unit="steps", total_tasks=32, successes=None,
                              source_result_id="range-run", budget={"max_tokens": 10000000})
        b.import_evidence(state, {"schema_version": 1, "observations": [], "aisi_findings": [finding]}, "Importer")
        result = self.preview(state, "aisi_cyber_range")
        self.assertIn("9.8 steps", result["text"])
        self.assertIn("mean_steps_completed", result["text"])
        self.assertIn("Scenario: Corporate range", result["text"])
        self.assertEqual("'AISI Evidence'!A5", result["location"])

    def test_changed_or_failed_source_cannot_look_like_current_accepted_evidence(self):
        state = fixture_state()
        b.merge_observations(state, [observation()])
        state = accept_pending(state)
        sole_entry(state)["source_review_required"] = True
        state["benchmarks"]["source_checks"]["failed"] = {
            "benchmark_id": "cybergym", "status": "unavailable"}
        result = self.preview(state)
        self.assertIn("40%", result["text"])
        self.assertIn("Source changed; review required", result["text"])
        self.assertIn("Source unavailable; retained values may be stale", result["text"])
        self.assertNotIn("Accepted evidence", result["text"])

    def test_failed_observation_watch_is_visible_without_aggregate_source_check(self):
        state = fixture_state()
        b.merge_observations(state, [observation()])
        sole_entry(state)["source_watch"] = {"status": "unavailable"}
        result = self.preview(state)
        self.assertIn("40%", result["text"])
        self.assertIn("Source unavailable; retained values may be stale", result["text"])

    def test_reported_budget_limits_preserve_zero_and_other_json_types(self):
        for limit, expected in (("USD 10", "USD 10"), (0, "0"), (100, "100"),
                                ([10, 20], "[10, 20]")):
            with self.subTest(limit=limit):
                state = fixture_state()
                b.merge_observations(state, [observation(budget={"reported_limit": limit}, agent=None, setting=None)])
                before = digest(state)
                result = self.preview(state)
                self.assertIn(expected, result["text"].splitlines())
                self.assertIn("40%", result["text"])
                self.assertEqual(before, digest(state))
                self.assertTrue(workbook_tables(state)[0]["rows"])

    def test_approximate_count_is_not_presented_as_exact_or_converted_to_percent(self):
        state = fixture_state()
        b.merge_observations(state, [observation(benchmark_id="exploitgym", value=368, unit="count",
                                                successes=None, total_tasks=None,
                                                conditions={"reported_counts_are_approximate": True,
                                                            "benchmark_total_tasks": 869})])
        text = self.preview(state, "exploitgym")["text"]
        self.assertIn("368 count (approximate)", text)
        self.assertNotIn("%", text)

    def test_missing_score_is_explicit_and_has_no_hyperlink(self):
        state = fixture_state()
        b.ensure_benchmarks(state)
        result = self.preview(state)
        self.assertEqual("No linked published result", result["text"])
        self.assertIsNone(result["location"])
        self.assertEqual([], result["observation_ids"])


class CoverageWorkbookRoundTrip(unittest.TestCase):
    def test_bundled_scores_links_and_numeric_details_survive_export_without_state_changes(self):
        from openpyxl import load_workbook
        from frontier_register.render import export_excel

        template = ROOT / "data/Organization_Frontier_Model_Register_2026-11-14.xlsx"
        state = import_workbook(template)
        for name in ("benchmark_results_seed.json", "benchmark_evidence_seed.json"):
            b.import_evidence(state, json.loads((ROOT / "data" / name).read_text()), "Bundled evidence")
        before = digest(state)
        decision_digests = [b.evidence_digest(r, state) for r in state["records"]]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "register.xlsx"
            export_excel(state, template, path)
            wb = load_workbook(path)
            ws = wb["Benchmark Coverage"]
            columns = {cell.value: cell.column for cell in ws[4]}
            rows = {ws.cell(row, 2).value: row for row in range(5, ws.max_row+1)}
            cell = ws.cell(rows["claude-opus-4-6"], columns["CVE-Bench"])
            self.assertIn("32.5%", cell.value)
            self.assertIn("40%", cell.value)
            self.assertIn("Zero-day", cell.value)
            self.assertIn("One-day", cell.value)
            self.assertIn("Pending review", cell.value)
            self.assertIsNotNone(cell.hyperlink)
            self.assertIsNone(cell.hyperlink.target)
            self.assertTrue(cell.hyperlink.location.startswith("'Benchmark Results'!A"))
            detail_row = int(cell.hyperlink.location.rsplit("A", 1)[1])
            details = wb["Benchmark Results"]
            self.assertEqual("claude-opus-4-6", details.cell(detail_row, 2).value)
            self.assertEqual(0.325, details.cell(detail_row, 5).value)
            self.assertEqual("0.00%", details.cell(detail_row, 5).number_format)
            candidate = ws.cell(rows["gemini-3.8-flash-cyber"], columns["CyberGym"])
            self.assertIn("86.26%", candidate.value)
            self.assertIn("Unconfirmed name/version match", candidate.value)
            scope_off = ws.cell(rows["claude-sonnet-4-5-20250929"], columns["Secure Code — Instruct"])
            self.assertIn("48.9%", scope_off.value)
            self.assertIn("Scope off; reference only", scope_off.value)
            self.assertEqual(305, details.max_row-4)
            self.assertEqual(52, wb["AISI Evidence"].max_row-4)
            self.assertEqual("D5", ws.freeze_panes)
            self.assertEqual("visible", ws.sheet_state)
            wb.close()
        self.assertEqual(before, digest(state))
        self.assertEqual(decision_digests, [b.evidence_digest(r, state) for r in state["records"]])


if __name__ == "__main__":
    unittest.main()
