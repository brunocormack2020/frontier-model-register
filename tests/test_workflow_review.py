"""Independent workflow invariants for governance and evidence changes."""
from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from frontier_register.core import (
    ORGANIZATION_FIELDS, VENDOR_FIELDS, RegisterError, StateLock, apply_organization_decisions,
    apply_fetch_results, apply_review, digest, make_review_queue, merge_discovery,
    monitoring, record_id, import_workbook, normalize_value, HEADERS, fields_for,
)
from frontier_register.render import confluence_fragment, export_excel


def seed_state() -> dict:
    vendor = {key: None for key in VENDOR_FIELDS}
    vendor.update({
        "Provider": "Google", "Exact Model ID": "gemini-test", "Model Family": "Test model",
        "Native Cyber Finding": "Unreviewed", "Original Suggested Treatment": "Original note",
    })
    organization = {key: None for key in ORGANIZATION_FIELDS}
    organization.update({"Organization Classification": "Pending classification", "Organization Frontier Determination": "Not determined"})
    rid = record_id("Google", "gemini-test")
    return {
        "schema_version": 1, "state_version": 0, "created_at": "2026-11-14T00:00:00Z",
        "updated_at": "2026-11-14T00:00:00Z", "audit": [], "sources": {}, "source_checks": {},
        "discovery_runs": [], "records": [{
            "record_id": rid, "sheet": "Model Register", "deployment_key": "unassigned",
            "vendor": vendor, "organization": organization, "metadata": {
                "origin": "workbook_import", "reviewed_source_hashes": {}, "pending_sources": [],
                "discovery_review_required": False, "decision_vendor_sha256": None,
            },
        }],
    }


def add_source(state: dict, raw_hash: str = "raw-v1", text_hash: str | None = "text-v1", status: str = "new") -> dict:
    rid = state["records"][0]["record_id"]
    state["sources"]["source-1"] = {"source_id": "source-1", "url": "https://example.test/card", "record_ids": [rid]}
    result = {
        "source_id": "source-1", "url": "https://example.test/card", "checked_at": "2026-11-14T01:00:00Z",
        "status": status, "raw_sha256": raw_hash, "text_sha256": text_hash,
        "raw_path": "evidence/raw.pdf", "text_path": "evidence/text.txt",
    }
    apply_fetch_results(state, [result])
    return result


def reviewed_state() -> dict:
    state = seed_state()
    add_source(state)
    queue = make_review_queue(state)
    queue["items"][0].update(action="keep_existing", review_note="Reviewed source against the retained statement.")
    return apply_review(state, queue, "reviewer")


class EvidenceWorkflowReview(unittest.TestCase):
    def test_raw_only_change_requires_reassessment(self):
        state = reviewed_state()
        before_organization = copy.deepcopy(state["records"][0]["organization"])
        add_source(state, raw_hash="raw-v2", text_hash="text-v1", status="changed")
        self.assertIn("source-1", state["records"][0]["metadata"]["pending_sources"])
        self.assertEqual(before_organization, state["records"][0]["organization"])
        self.assertIn("Reassessment", monitoring(state["records"][0], state)[0])

    def test_new_source_without_extracted_text_still_needs_review(self):
        state = seed_state()
        add_source(state, text_hash=None)
        self.assertIn("source-1", state["records"][0]["metadata"]["pending_sources"])
        self.assertEqual(1, len(make_review_queue(state)["items"]))

    def test_failed_fetch_keeps_finding_and_warns(self):
        state = reviewed_state()
        original = copy.deepcopy(state["records"][0])
        add_source(state, status="unavailable")
        self.assertEqual(original["vendor"], state["records"][0]["vendor"])
        self.assertEqual(original["organization"], state["records"][0]["organization"])
        self.assertIn("unavailable", monitoring(state["records"][0], state)[0])

    def test_stale_review_rejected_without_mutation(self):
        state = seed_state()
        add_source(state)
        queue = make_review_queue(state)
        queue["items"][0].update(action="keep_existing", review_note="Reviewed.")
        add_source(state, raw_hash="raw-v2", text_hash="text-v2", status="changed")
        before = copy.deepcopy(state)
        with self.assertRaises(RegisterError):
            apply_review(state, queue, "reviewer")
        self.assertEqual(before, state)

    def test_review_cannot_write_organization_decision_or_identity(self):
        for protected in ("Organization Classification", "Exact Model ID", "Original Suggested Treatment"):
            with self.subTest(protected=protected):
                state = seed_state()
                add_source(state)
                queue = make_review_queue(state)
                queue["items"][0].update(action="update", review_note="Proposed update.", proposed_updates={protected: "changed"})
                before = copy.deepcopy(state)
                with self.assertRaises(RegisterError):
                    apply_review(state, queue, "reviewer")
                self.assertEqual(before, state)

    def test_forged_source_digest_rejected(self):
        state = seed_state()
        add_source(state)
        queue = make_review_queue(state)
        queue["items"][0].update(action="keep_existing", review_note="Reviewed.")
        queue["items"][0]["sources"][0]["raw_sha256"] = "different-raw-evidence"
        with self.assertRaises(RegisterError):
            apply_review(state, queue, "reviewer")

    def test_administrative_organization_edit_cannot_reapprove_changed_evidence(self):
        state = seed_state()
        record = state["records"][0]
        record["organization"].update({
            "Organization Classification": "Eligible for assessment", "Organization Frontier Determination": "Outside exclusion",
            "Organization Rationale / Open Decision": "Earlier finding accepted.", "Decision Owner": "Model committee",
            "Decision Date": "2026-11-01", "Approval Record": "DEC-123",
        })
        old_binding = digest(record["vendor"])
        record["metadata"]["decision_vendor_sha256"] = old_binding
        record["vendor"]["Native Cyber Finding"] = "A newly reported capability finding"
        request = {"state_sha256": digest(state), "items": [{
            "record_id": record["record_id"], "updates": {"Next Review Date": "2026-12-01"},
        }]}
        try:
            result = apply_organization_decisions(state, request, "operator")
        except RegisterError:
            return  # Requiring an explicit decision transaction is also safe.
        self.assertEqual(old_binding, result["records"][0]["metadata"]["decision_vendor_sha256"])
        self.assertIn("reassessment", monitoring(result["records"][0], result)[0].lower())

    def test_same_name_catalog_version_change_requires_review(self):
        state = seed_state()
        observed = {"provider": "Google", "model_id": "gemini-test", "display_name": "Test model", "description": "",
                    "api_metadata": {"version": "001", "baseModelId": "gemini-base-1"}, "checked_at": "2026-11-14T01:00:00Z"}
        merge_discovery(state, "Google", [observed], ["gemini-"])
        # Treat the first observation as acknowledged so this tests the later change.
        state["records"][0]["metadata"]["discovery_review_required"] = False
        observed2 = copy.deepcopy(observed)
        observed2["api_metadata"]["version"] = "002"
        observed2["checked_at"] = "2026-11-15T01:00:00Z"
        original_vendor = copy.deepcopy(state["records"][0]["vendor"])
        merge_discovery(state, "Google", [observed2], ["gemini-"])
        self.assertTrue(state["records"][0]["metadata"]["discovery_review_required"])
        self.assertEqual(original_vendor, state["records"][0]["vendor"])
        self.assertEqual(1, len(state["records"]))

    def test_discovery_absence_never_retires_a_model(self):
        state = seed_state()
        record_before = copy.deepcopy(state["records"][0])
        merge_discovery(state, "Google", [], ["gemini-"])
        self.assertEqual(record_before, state["records"][0])

    def test_new_discovery_defaults_to_pending_and_unknown(self):
        state = seed_state()
        merge_discovery(state, "Google", [{"model_id": "gemini-new", "display_name": "New model", "description": "Vendor description"}], ["gemini-"])
        record = next(r for r in state["records"] if r["vendor"]["Exact Model ID"] == "gemini-new")
        self.assertEqual("Pending classification", record["organization"]["Organization Classification"])
        self.assertEqual("Not determined", record["organization"]["Organization Frontier Determination"])
        self.assertTrue(record["metadata"]["discovery_review_required"])

    def test_overlapping_writer_refused_and_lock_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            with StateLock(Path(tmp)):
                with self.assertRaises(RegisterError):
                    with StateLock(Path(tmp)):
                        pass
            with StateLock(Path(tmp)):
                self.assertTrue((Path(tmp) / ".register.lock").exists())

    def test_review_then_admin_edit_keeps_explicit_decision_stale(self):
        state = reviewed_state()
        record = state["records"][0]
        state = apply_organization_decisions(state, {"state_sha256": digest(state), "items": [{
            "record_id": record["record_id"], "confirm_decision": True,
            "expected_vendor_sha256": digest(record["vendor"]), "updates": {
                "Organization Classification": "Eligible for assessment", "Organization Frontier Determination": "Outside exclusion",
                "Organization Rationale / Open Decision": "Reviewed under the organization exclusion definition.",
                "Decision Owner": "Model committee", "Decision Date": "2026-11-14", "Approval Record": "DEC-001",
            },
        }]}, "decision-maker")
        binding = state["records"][0]["metadata"]["decision_vendor_sha256"]
        add_source(state, raw_hash="raw-v2", text_hash="text-v2", status="changed")
        queue = make_review_queue(state)
        queue["items"][0].update(action="update", review_note="New model threshold finding reviewed.",
                                  proposed_updates={"Native Cyber Finding": "A changed vendor threshold finding"})
        state = apply_review(state, queue, "evidence-reviewer")
        state = apply_organization_decisions(state, {"state_sha256": digest(state), "items": [{
            "record_id": state["records"][0]["record_id"], "updates": {"Next Review Date": "2026-12-01"},
        }]}, "administrator")
        self.assertEqual(binding, state["records"][0]["metadata"]["decision_vendor_sha256"])
        self.assertIn("Organization decision requires reassessment", monitoring(state["records"][0], state)[0])

    def test_same_catalog_content_new_observation_time_is_not_a_change(self):
        state = seed_state()
        observation = {"provider": "Google", "model_id": "gemini-test", "display_name": "Test",
                       "description": "Description", "api_metadata": {"version": "001"},
                       "checked_at": "2026-11-14T01:00:00Z"}
        merge_discovery(state, "Google", [observation], ["gemini-"])
        state["records"][0]["metadata"]["discovery_review_required"] = False
        observation2 = copy.deepcopy(observation)
        observation2["checked_at"] = "2026-11-15T01:00:00Z"
        merge_discovery(state, "Google", [observation2], ["gemini-"])
        self.assertFalse(state["records"][0]["metadata"]["discovery_review_required"])

    def test_changed_deployment_scope_remains_visible_after_admin_edit(self):
        state = reviewed_state()
        record = state["records"][0]
        state = apply_organization_decisions(state, {"state_sha256": digest(state), "items": [{
            "record_id": record["record_id"], "confirm_decision": True,
            "expected_vendor_sha256": digest(record["vendor"]), "updates": {
                "Organization Classification": "Eligible for assessment", "Organization Frontier Determination": "Outside exclusion",
                "Organization Rationale / Open Decision": "Accepted only with read-only tools.",
                "Organization Use / Data / Tools": "Read-only tools", "Decision Owner": "Committee",
                "Decision Date": "2026-11-14", "Approval Record": "DEC-002",
            },
        }]}, "decision-maker")
        state = apply_organization_decisions(state, {"state_sha256": digest(state), "items": [{
            "record_id": state["records"][0]["record_id"],
            "updates": {"Organization Use / Data / Tools": "Write-enabled privileged tools"},
        }]}, "administrator")
        self.assertIn("reassessment", monitoring(state["records"][0], state)[0].lower())

    def test_raw_document_review_requires_explicit_acknowledgment(self):
        state = seed_state()
        add_source(state, text_hash=None)
        queue = make_review_queue(state)
        queue["items"][0].update(action="keep_existing", review_note="Manual PDF review completed.")
        with self.assertRaises(RegisterError):
            apply_review(state, queue, "reviewer")
        queue["items"][0]["raw_evidence_reviewed"] = True
        result = apply_review(state, queue, "reviewer")
        self.assertEqual([], result["records"][0]["metadata"]["pending_sources"])


class ExportIntegrationReview(unittest.TestCase):
    def test_successful_publication_preserves_review_state_digest(self):
        import yaml
        from frontier_register.cli import main
        from frontier_register.confluence import PublishResult
        state = seed_state()
        add_source(state)
        queue_hash = make_review_queue(state)["state_sha256"]
        cfg = {
            "schema_version": 1, "work_dir": "work", "output_dir": "out",
            "allowed_source_hosts": ["example.test"],
            "confluence": {"enabled": True, "mode": "cloud", "base_url": "https://example.atlassian.net/wiki", "page_id": "123"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(yaml.safe_dump(cfg))
            work = Path(tmp) / "work"
            work.mkdir()
            state_path = work / "register.json"
            state_path.write_text(json.dumps(state))
            before = state_path.read_bytes()
            with patch("frontier_register.cli.ConfluencePublisher") as publisher, redirect_stdout(io.StringIO()):
                publisher.return_value.publish.return_value = PublishResult("published", "123", 1, 2, True, "page-hash", "<p>Managed preview</p>")
                self.assertEqual(0, main(["--config", str(config), "publish", "--apply"]))
                fragment_first = publisher.return_value.publish.call_args.args[0]
                publisher.return_value.publish.return_value = PublishResult("unchanged", "123", 2, None, False, "page-hash", "<p>Managed preview</p>")
                self.assertEqual(0, main(["--config", str(config), "publish", "--apply"]))
                fragment_second = publisher.return_value.publish.call_args.args[0]
            self.assertEqual(before, state_path.read_bytes())
            self.assertEqual(fragment_first, fragment_second)
            self.assertEqual(queue_hash, digest(json.loads(state_path.read_text())))
            history = json.loads((work / "publication_history.json").read_text())
            self.assertEqual(1, len(history["events"]))

    def test_confluence_output_treats_vendor_markup_as_literal_text(self):
        class Tags(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []
                self.attrs = []
            def handle_starttag(self, tag, attrs):
                self.tags.append(tag)
                self.attrs.extend(attrs)

        state = seed_state()
        record = state["records"][0]
        record["vendor"]["Model Family"] = '<script>alert("not executable")</script>'
        record["vendor"]["Vendor-stated Strengths"] = '<img src=x onerror="alert(1)">'
        record["vendor"]["Primary Evidence (URL)"] = 'https://example.test/?x=" onmouseover="alert(1)'
        body = confluence_fragment(state)
        parser = Tags()
        parser.feed(body)
        self.assertNotIn("script", parser.tags)
        self.assertNotIn("img", parser.tags)
        self.assertFalse(any(key.lower().startswith("on") for key, value in parser.attrs))
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("Pending classification", body)

    def test_excel_keeps_all_original_fields_and_neutralizes_formula_text(self):
        from openpyxl import load_workbook
        root = Path(__file__).resolve().parents[1]
        template = root / "data" / "Organization_Frontier_Model_Register_2026-11-14.xlsx"
        state = import_workbook(template)
        attack = '=HYPERLINK("https://example.test", "Do not execute")'
        state["records"][0]["vendor"]["Vendor-stated Strengths"] = attack
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "register.xlsx"
            export_excel(state, template, path)
            wb = load_workbook(path, data_only=False)
            try:
                self.assertEqual(attack, wb["Model Register"]["E5"].value)
                self.assertEqual("s", wb["Model Register"]["E5"].data_type)
                for sheet_name in ("Model Register", "Prior and Special"):
                    ws = wb[sheet_name]
                    records = [r for r in state["records"] if r["sheet"] == sheet_name]
                    self.assertEqual(HEADERS, [ws.cell(4, i).value for i in range(1, 39)])
                    for row_number, record in enumerate(records, 5):
                        expected = fields_for(record)
                        for column, header in enumerate(HEADERS, 1):
                            self.assertEqual(expected.get(header), normalize_value(ws.cell(row_number, column).value),
                                             f"{sheet_name}:{row_number}:{header}")
                    self.assertEqual("D5", ws.freeze_panes)
                    self.assertEqual(f"A4:AR{len(records)+4}", next(iter(ws.tables.values())).ref)
                self.assertIn("Onboarding Rules", wb.sheetnames)
                self.assertIn("Field Guide", wb.sheetnames)
                self.assertIn("Source Monitor", wb.sheetnames)
                self.assertEqual("Rule", wb["Onboarding Rules"]["A4"].value)
            finally:
                wb.close()

    def test_offline_cli_init_export_and_reimport_protection(self):
        import yaml
        from frontier_register.cli import main
        root = Path(__file__).resolve().parents[1]
        template = root / "data" / "Organization_Frontier_Model_Register_2026-11-14.xlsx"
        cfg = yaml.safe_load((root / "config.example.yaml").read_text())
        cfg["benchmarks"]["seed_files"] = [str(root / value) for value in cfg["benchmarks"]["seed_files"]]
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(yaml.safe_dump(cfg))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(0, main(["--config", str(config), "init", "--workbook", str(template)]))
                self.assertEqual(0, main(["--config", str(config), "export"]))
                state_path = Path(tmp) / "work" / "register.json"
                before = state_path.read_bytes()
                self.assertEqual(1, main(["--config", str(config), "init", "--workbook", str(template)]))
            self.assertEqual(before, state_path.read_bytes())
            summary = json.loads((Path(tmp) / "out" / "run_summary.json").read_text())
            self.assertEqual(65, summary["records"])
            self.assertEqual(23, summary["current_records"])
            self.assertTrue((Path(tmp) / "out" / "model_register.xlsx").is_file())
            self.assertTrue((Path(tmp) / "out" / "confluence_fragment.html").is_file())


if __name__ == "__main__":
    unittest.main()
