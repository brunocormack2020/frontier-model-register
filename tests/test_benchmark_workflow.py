"""Independent invariants for published benchmark evidence and organization decisions."""
from __future__ import annotations

import copy
from html.parser import HTMLParser
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from frontier_register import benchmarks as b
from frontier_register.core import (
    ORGANIZATION_FIELDS, VENDOR_FIELDS, HEADERS, RegisterError, apply_organization_decisions,
    digest, fields_for, import_workbook, monitoring, record_id,
)


def fixture_state() -> dict:
    vendor = {key: None for key in VENDOR_FIELDS}
    vendor.update({"Provider": "Google", "Exact Model ID": "gemini-test-001",
                   "Model Family": "Gemini Test", "Version": "001",
                   "Native Cyber Finding": "Previously recorded finding",
                   "Original Suggested Treatment": "Original note"})
    organization = {key: None for key in ORGANIZATION_FIELDS}
    organization.update({"Organization Classification": "Pending classification",
                 "Organization Frontier Determination": "Not determined"})
    return {"schema_version": 1, "state_version": 0, "created_at": "2026-11-14T00:00:00Z",
            "updated_at": "2026-11-14T00:00:00Z", "audit": [], "sources": {},
            "source_checks": {}, "discovery_runs": [], "records": [{
                "record_id": record_id("Google", "gemini-test-001"), "sheet": "Model Register",
                "deployment_key": "unassigned", "vendor": vendor, "organization": organization,
                "metadata": {"origin": "workbook_import", "reviewed_source_hashes": {},
                             "pending_sources": [], "discovery_review_required": False,
                             "decision_vendor_sha256": None}}]}


def observation(**changes) -> dict:
    data = {"benchmark_id": "cybergym", "provider": "Google",
            "reported_model": "Gemini Test 001", "model_id": "gemini-test-001",
            "agent": "Fixture agent", "metric": "success_rate", "value": 40.0,
            "unit": "percent", "successes": 4, "total_tasks": 10, "attempts": 1,
            "benchmark_version": "fixture-v1", "dataset_subset": "Ten fixture tasks",
            "setting": "level_1", "budget": {"max_turns": 100},
            "safeguards": "Standard endpoint", "mitigations": "Not specified",
            "source_url": "https://www.cybergym.io/cybergym/", "source_result_id": "fixture-run-1",
            "source_revision": "fixture-revision-1", "source_sha256": "a" * 64,
            "evaluation_date": "2026-11-13", "published_date": "2026-11-14",
            "retrieved_at": "2026-11-14T00:00:00Z", "evaluator": "Fixture evaluator",
            "notes": "Test observation only", "metric_direction": "capability",
            "category": "dual_use", "conditions": {"hints": False}}
    data.update(changes)
    return data


def sole_entry(state: dict, kind: str = "benchmark") -> dict:
    entries = list(b.iter_entries(state, kind))
    assert len(entries) == 1
    return entries[0][1]


def accept_pending(state: dict, **item_changes) -> dict:
    queue = b.make_benchmark_review_queue(state)
    for item in queue["items"]:
        item.update(action="accept", review_note="Checked the source, metric and evaluation setup.")
        item.update(item_changes)
    return b.apply_benchmark_review(state, queue, "Evidence reviewer")


def confirm_organization_decision(state: dict) -> dict:
    record = state["records"][0]
    request = {"state_sha256": digest(state), "items": [{
        "record_id": record["record_id"], "confirm_decision": True,
        "expected_vendor_sha256": digest(record["vendor"]),
        "expected_benchmark_sha256": b.evidence_digest(record, state),
        "updates": {"Organization Classification": "Eligible for assessment",
                    "Organization Frontier Determination": "Outside exclusion",
                    "Organization Rationale / Open Decision": "Documented evidence assessed under the organization definition.",
                    "Decision Owner": "Model committee", "Decision Date": "2026-11-14",
                    "Approval Record": "DEC-BENCH-001"}}]}
    return apply_organization_decisions(state, request, "Decision owner")


class BenchmarkMigrationAndMapping(unittest.TestCase):
    def test_extension_is_additive_and_idempotent(self):
        state = fixture_state()
        records = copy.deepcopy(state["records"])
        b.ensure_benchmarks(state)
        before = copy.deepcopy(state)
        b.ensure_benchmarks(state)
        self.assertEqual(before, state)
        self.assertEqual(records, state["records"])
        self.assertEqual({"coding": False, "soc": False}, state["benchmarks"]["scope"])
        self.assertEqual(7, len(b.CORE))

    def test_new_exact_identifier_result_is_pending_without_changing_organization(self):
        state = fixture_state()
        records = copy.deepcopy(state["records"])
        result = b.merge_observations(state, [observation()])
        entry = sole_entry(state)
        self.assertEqual(1, result["new"])
        self.assertEqual("exact_id", entry["mapping_status"])
        self.assertEqual(state["records"][0]["record_id"], entry["record_id"])
        self.assertEqual("pending", entry["review"]["status"])
        self.assertEqual(records, state["records"])
        self.assertIn("review required", b.monitoring(state["records"][0], state))

    def test_name_match_is_only_a_suggestion(self):
        state = fixture_state()
        b.merge_observations(state, [observation(model_id=None)])
        entry = sole_entry(state)
        self.assertIsNone(entry["record_id"])
        self.assertEqual("unmapped", entry["mapping_status"])
        self.assertTrue(b.candidate_matches(state, entry["data"]))
        self.assertIn("mapping unconfirmed", b.coverage(state, state["records"][0], "cybergym"))

    def test_matching_does_not_cross_provider_or_model_variant(self):
        for provider, model_id in [("Anthropic", "gemini-test-001"),
                                   ("Google", "gemini-test-mini-001"),
                                   ("Google", "gemini-test-002")]:
            with self.subTest(provider=provider, model_id=model_id):
                state = fixture_state()
                b.merge_observations(state, [observation(provider=provider, model_id=model_id)])
                self.assertIsNone(sole_entry(state)["record_id"])

    def test_reviewed_label_mapping_requires_reason_and_preserves_uncertainty(self):
        state = fixture_state()
        b.merge_observations(state, [observation(model_id=None)])
        queue = b.make_benchmark_review_queue(state)
        item = queue["items"][0]
        item.update(action="accept", record_id=state["records"][0]["record_id"],
                    review_note="Reviewed the source's reported label.")
        with self.assertRaises(RegisterError):
            b.apply_benchmark_review(state, queue, "Reviewer")
        item["mapping_basis"] = "Label corresponds to this register entry; exact underlying snapshot was not disclosed."
        mapped = b.apply_benchmark_review(state, queue, "Reviewer")
        self.assertEqual("reviewed_label", sole_entry(mapped)["mapping_status"])
        self.assertIsNone(sole_entry(mapped)["data"]["model_id"])

    def test_review_cannot_reassign_disclosed_model_id(self):
        state = fixture_state()
        b.merge_observations(state, [observation(model_id="gemini-test-mini-001")])
        queue = b.make_benchmark_review_queue(state)
        queue["items"][0].update(action="accept", record_id=state["records"][0]["record_id"],
                                  mapping_basis="An impermissible family-level mapping.", review_note="Reviewed.")
        with self.assertRaises(RegisterError):
            b.apply_benchmark_review(state, queue, "Reviewer")


class BenchmarkEvidenceTransactions(unittest.TestCase):
    def test_failed_source_preserves_previous_observation(self):
        state = fixture_state()
        source = {"source_id": "source-1", "benchmark_id": "cybergym",
                  "url": "https://www.cybergym.io/cybergym/", "status": "succeeded",
                  "checked_at": "2026-11-14T01:00:00Z"}
        b.merge_observations(state, [observation()], [source])
        state = accept_pending(state)
        original = copy.deepcopy(sole_entry(state))
        source.update(status="unavailable", error="Timed out", checked_at="2026-11-15T01:00:00Z")
        b.merge_observations(state, [], [source])
        self.assertEqual(original, sole_entry(state))
        check = state["benchmarks"]["source_checks"]["source-1"]
        self.assertEqual("succeeded", check["last_success"]["status"])
        self.assertEqual(1, b.summary(state)["benchmark_source_errors"])
        self.assertIn("source unavailable", b.monitoring(state["records"][0], state).lower())
        self.assertIn("source unavailable", b.coverage(state, state["records"][0], "cybergym"))

    def test_retrieval_only_changes_do_not_reopen_review(self):
        state = fixture_state()
        b.merge_observations(state, [observation()])
        state = accept_pending(state)
        sha = sole_entry(state)["content_sha256"]
        result = b.merge_observations(state, [observation(retrieved_at="2026-11-15T00:00:00Z",
                                                          source_sha256="b" * 64,
                                                          source_revision="new-source-commit")])
        self.assertEqual(1, result["unchanged"])
        self.assertEqual(sha, sole_entry(state)["content_sha256"])
        self.assertEqual("accepted", sole_entry(state)["review"]["status"])
        self.assertEqual([], b.make_benchmark_review_queue(state)["items"])

    def test_corrected_score_retains_prior_review_in_history(self):
        state = fixture_state()
        b.merge_observations(state, [observation()])
        state = accept_pending(state)
        result = b.merge_observations(state, [observation(value=30.0, successes=3)])
        entry = sole_entry(state)
        self.assertEqual(1, result["changed"])
        self.assertEqual("pending", entry["review"]["status"])
        self.assertEqual(40.0, entry["history"][0]["data"]["value"])
        self.assertEqual("accepted", entry["history"][0]["review"]["status"])

    def test_distinct_runs_and_metrics_remain_separate(self):
        state = fixture_state()
        b.merge_observations(state, [observation(),
                                    observation(source_result_id="fixture-run-2", value=80.0,
                                                successes=8, budget={"max_turns": 1000}),
                                    observation(metric="subtask_rate", value=60.0, successes=None)])
        self.assertEqual(3, len(state["benchmarks"]["observations"]))
        self.assertEqual({40.0, 60.0, 80.0}, {entry["data"]["value"] for _, entry in b.iter_entries(state)})

    def test_stale_review_is_rejected_without_mutation(self):
        state = fixture_state()
        b.merge_observations(state, [observation()])
        queue = b.make_benchmark_review_queue(state)
        queue["items"][0].update(action="accept", review_note="Reviewed.")
        b.merge_observations(state, [observation(value=50.0, successes=5)])
        before = copy.deepcopy(state)
        with self.assertRaises(RegisterError):
            b.apply_benchmark_review(state, queue, "Reviewer")
        self.assertEqual(before, state)

    def test_forged_content_or_published_value_cannot_be_reviewed(self):
        for mutation in ("hash", "value", "organization"):
            with self.subTest(mutation=mutation):
                state = fixture_state()
                b.merge_observations(state, [observation()])
                queue = b.make_benchmark_review_queue(state)
                item = queue["items"][0]
                item.update(action="accept", review_note="Reviewed.")
                if mutation == "hash":
                    item["expected_content_sha256"] = "forged"
                elif mutation == "value":
                    item["published_result"]["value"] = 1.0
                    # The generated queue must not provide an alias into state.
                else:
                    item["Organization Classification"] = "Eligible for assessment"
                before = copy.deepcopy(state)
                with self.assertRaises(RegisterError):
                    b.apply_benchmark_review(state, queue, "Reviewer")
                self.assertEqual(before, state)

    def test_review_queue_is_detached_from_authoritative_observations(self):
        state = fixture_state()
        b.merge_observations(state, [observation()])
        before = copy.deepcopy(state)
        queue = b.make_benchmark_review_queue(state)
        queue["items"][0]["published_result"]["value"] = 99.0
        self.assertEqual(before, state)

    def test_rejection_retains_the_published_value(self):
        state = fixture_state()
        b.merge_observations(state, [observation()])
        queue = b.make_benchmark_review_queue(state)
        queue["items"][0].update(action="reject", review_note="The setting is not suitable for the intended comparison.")
        state = b.apply_benchmark_review(state, queue, "Reviewer")
        self.assertEqual("rejected", sole_entry(state)["review"]["status"])
        self.assertEqual(40.0, sole_entry(state)["data"]["value"])
        self.assertEqual([], b.make_benchmark_review_queue(state)["items"])

    def test_numeric_zero_is_valid_but_missing_or_invalid_score_is_not(self):
        state = fixture_state()
        b.merge_observations(state, [observation(value=0.0, successes=0)])
        self.assertEqual(0.0, sole_entry(state)["data"]["value"])
        for bad in (None, "0", True, -1, float("nan"), float("inf"), 101):
            with self.subTest(value=bad):
                state = fixture_state()
                before = copy.deepcopy(state)
                with self.assertRaises((RegisterError, ValueError)):
                    b.merge_observations(state, [observation(value=bad)])
                self.assertEqual(before, state)

    def test_invalid_later_row_does_not_partially_commit(self):
        state = fixture_state()
        before = copy.deepcopy(state)
        with self.assertRaises(RegisterError):
            b.merge_observations(state, [observation(), observation(source_result_id="bad", provider="Other vendor")])
        self.assertEqual(before, state)

    def test_invalid_later_evidence_category_does_not_partially_commit(self):
        state = fixture_state()
        before = copy.deepcopy(state)
        with self.assertRaises(RegisterError):
            b.import_evidence(state, {"schema_version": 1, "observations": [observation()],
                                      "aisi_findings": [observation()]}, "Importer")
        self.assertEqual(before, state)

    def test_duplicate_observation_identity_is_rejected_atomically(self):
        state = fixture_state()
        before = copy.deepcopy(state)
        with self.assertRaises(RegisterError):
            b.merge_observations(state, [observation(), observation(value=50.0, successes=5)])
        self.assertEqual(before, state)


class BenchmarkScopeAndDecisions(unittest.TestCase):
    def test_conditional_scope_preserves_results_and_distinguishes_unselected(self):
        state = fixture_state()
        b.merge_observations(state, [observation(benchmark_id="secure_code_instruct",
                                                metric="vulnerable_percentage", metric_direction="lower_is_better")])
        state = accept_pending(state)
        entry = copy.deepcopy(sole_entry(state))
        self.assertIn("scope not enabled", b.coverage(state, state["records"][0], "secure_code_instruct"))
        before = b.evidence_digest(state["records"][0], state)
        b.set_scope(state, coding=True, soc=False, actor="Scope owner")
        self.assertTrue(b.is_active(state, "secure_code_instruct"))
        self.assertFalse(b.is_active(state, "cybersoceval_malware"))
        self.assertIn("1 reviewed", b.coverage(state, state["records"][0], "secure_code_instruct"))
        self.assertNotEqual(before, b.evidence_digest(state["records"][0], state))
        self.assertEqual(entry, sole_entry(state))

    def test_scope_requires_explicit_booleans(self):
        state = fixture_state()
        with self.assertRaises(RegisterError):
            b.set_scope(state, coding="false", soc=False, actor="Owner")
        with self.assertRaises(RegisterError):
            b.ensure_benchmarks(state, {"coding_in_scope": "false"})

    def test_aisi_findings_remain_a_separate_evidence_category(self):
        state = fixture_state()
        finding = observation(scenario="Corporate range", metric="mean_steps_completed", value=9.8,
                              unit="steps", total_tasks=32, successes=None, source_result_id="range-10m",
                              budget={"max_tokens": 10000000}, active_defenders=False)
        b.import_evidence(state, {"schema_version": 1, "observations": [observation()],
                                  "aisi_findings": [finding]}, "Importer")
        self.assertEqual(1, len(state["benchmarks"]["observations"]))
        self.assertEqual(1, len(state["benchmarks"]["aisi_findings"]))
        entry = sole_entry(state, "aisi")
        self.assertEqual("aisi_cyber_range", entry["data"]["benchmark_id"])
        self.assertEqual("Corporate range", entry["data"]["scenario"])
        self.assertEqual(9.8, entry["data"]["value"])
        self.assertFalse(entry["data"]["active_defenders"])

    def test_organization_decision_binding_survives_refresh_and_requires_reassessment(self):
        state = fixture_state()
        b.merge_observations(state, [observation()])
        state = accept_pending(state)
        state = confirm_organization_decision(state)
        record = state["records"][0]
        prior_organization = copy.deepcopy(record["organization"])
        binding = record["metadata"].get("decision_benchmark_sha256")
        self.assertEqual(b.evidence_digest(record, state), binding)
        b.merge_observations(state, [observation(value=80.0, successes=8)])
        self.assertEqual(prior_organization, state["records"][0]["organization"])
        self.assertIn("benchmark", monitoring(state["records"][0], state)[0].lower())
        state = accept_pending(state)
        self.assertIn("benchmark reassessment", b.monitoring(state["records"][0], state))
        request = {"state_sha256": digest(state), "items": [{"record_id": state["records"][0]["record_id"],
                   "updates": {"Next Review Date": "2026-12-01"}}]}
        state = apply_organization_decisions(state, request, "Administrator")
        self.assertEqual(binding, state["records"][0]["metadata"]["decision_benchmark_sha256"])
        self.assertIn("benchmark reassessment", b.monitoring(state["records"][0], state))

    def test_accepting_unmapped_evidence_does_not_change_model_evidence(self):
        state = fixture_state()
        b.ensure_benchmarks(state)
        initial = b.evidence_digest(state["records"][0], state)
        b.merge_observations(state, [observation(model_id=None, reported_model="Unknown test release")])
        state = accept_pending(state)
        self.assertIsNone(sole_entry(state)["record_id"])
        self.assertEqual(initial, b.evidence_digest(state["records"][0], state))


class BenchmarkWorkbookPreservation(unittest.TestCase):
    def test_benchmark_confluence_content_escapes_source_markup(self):
        from frontier_register.render import confluence_fragment

        class Tags(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []
                self.attrs = []

            def handle_starttag(self, tag, attrs):
                self.tags.append(tag)
                self.attrs.extend(attrs)

        state = fixture_state()
        b.merge_observations(state, [observation(reported_model='<script>alert("source")</script>',
                                                agent='<img src=x onerror="alert(1)">')])
        body = confluence_fragment(state)
        tags = Tags()
        tags.feed(body)
        self.assertNotIn("script", tags.tags)
        self.assertNotIn("img", tags.tags)
        self.assertFalse(any(name.startswith("on") for name, _ in tags.attrs))
        self.assertIn("&lt;script&gt;", body)

    def test_benchmark_excel_source_text_is_never_a_formula(self):
        from frontier_register.render import export_excel
        from openpyxl import load_workbook

        seed = Path(__file__).resolve().parents[1] / "data" / "Organization_Frontier_Model_Register_2026-11-14.xlsx"
        state = import_workbook(seed)
        formula_like = '=HYPERLINK("https://example.invalid","source label")'
        b.merge_observations(state, [observation(reported_model=formula_like, model_id=None)])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.xlsx"
            export_excel(state, seed, path)
            wb = load_workbook(path, read_only=True, data_only=False)
            try:
                cells = [cell for row in wb["Benchmark Results"].iter_rows() for cell in row if cell.value == formula_like]
                self.assertTrue(cells, "Published source label should be preserved in benchmark results")
                self.assertTrue(all(cell.data_type == "s" for cell in cells))
            finally:
                wb.close()

    def test_extension_export_preserves_all_original_records_and_fields(self):
        from frontier_register.render import export_excel
        from openpyxl import load_workbook

        seed = Path(__file__).resolve().parents[1] / "data" / "Organization_Frontier_Model_Register_2026-11-14.xlsx"
        state = import_workbook(seed)
        original = {r["record_id"]: copy.deepcopy(fields_for(r)) for r in state["records"]}
        first = state["records"][0]
        b.merge_observations(state, [observation(provider=first["vendor"]["Provider"],
                                                model_id=first["vendor"]["Exact Model ID"],
                                                reported_model="Published test label")])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.xlsx"
            export_excel(state, seed, path)
            imported = import_workbook(path)
            actual = {r["record_id"]: fields_for(r) for r in imported["records"]}
            self.assertEqual(65, len(actual))
            self.assertEqual(original, actual)
            wb = load_workbook(path, read_only=True, data_only=False)
            try:
                self.assertEqual(HEADERS, [c.value for c in next(wb["Model Register"].iter_rows(min_row=4, max_row=4, max_col=38))])
                self.assertIn("Benchmark Results", wb.sheetnames)
                self.assertIn("AISI Evidence", wb.sheetnames)
            finally:
                wb.close()


if __name__ == "__main__":
    unittest.main()
