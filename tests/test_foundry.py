"""Offline tests for read-only ARM/Foundry deployment inventory and governance boundaries."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from frontier_register import foundry as F
from frontier_register import identity as D
from frontier_register import intelligence as I

SUB = "00000000-1111-2222-3333-444444444444"
ARM_ACCOUNT = f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/Microsoft.CognitiveServices/accounts/foundrybank"
PROJECT = "https://foundrybank.services.ai.azure.com/api/projects/security"


class Credential:
    def __init__(self):
        self.scopes = []
    def get_token(self, scope):
        self.scopes.append(scope)
        return SimpleNamespace(token="FAKE_TOKEN")


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
    @contextmanager
    def stream(self, method, url, headers=None, timeout=None, follow_redirects=None):
        self.calls.append((method, url, headers, follow_redirects))
        if not self.responses:
            raise AssertionError("Unexpected additional Foundry HTTP request")
        status, body = self.responses.pop(0)
        import json
        raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
        yield SimpleNamespace(status_code=status, headers={"content-type": "application/json"},
                              iter_bytes=lambda: iter([raw]))


def cfg(**kwargs):
    return {"enabled": True, "subscriptions": [], "accounts": [], "projects": [],
            "max_pages": 5, "max_accounts": 10, "max_deployments": 20,
            "max_response_bytes": 30000, "timeout_seconds": 5, **kwargs}


def arm(name="deployment-1", version="2025-04-14", model="gpt-4.1", publisher="OpenAI"):
    return {"id": f"{ARM_ACCOUNT}/deployments/{name}", "name": name,
            "properties": {"model": {"name": model, "version": version, "format": publisher},
                           "provisioningState": "Succeeded", "versionUpgradeOption": "NoAutoUpgrade",
                           "raiPolicyName": "bank-policy"},
            "sku": {"name": "GlobalStandard", "capacity": 5}}


def proj(name="deployment-1", model="gpt-4.1", version="2025-04-14", publisher="OpenAI"):
    return {"name": name, "modelName": model, "modelVersion": version,
            "modelPublisher": publisher, "type": "ModelDeployment", "sku": {"name": "GlobalStandard"}}


class ValidationAndCollection(unittest.TestCase):
    def test_rejects_invalid_project_urls_and_duplicate_scopes(self):
        for endpoint in ("http://foundrybank.services.ai.azure.com/api/projects/demo",
                         "https://evil.example/api/projects/security",
                         "https://foundrybank.services.ai.azure.com/api/projects/demo?x=1",
                         "https://foundrybank.services.ai.azure.com/api/projects/../other",
                         "https://user:secret@foundrybank.services.ai.azure.com/api/projects/demo"):
            with self.subTest(endpoint=endpoint), self.assertRaises(F.FoundryError):
                F.validate_config(cfg(projects=[endpoint]))
        with self.assertRaises(F.FoundryError):
            F.validate_config(cfg(subscriptions=[SUB], accounts=[ARM_ACCOUNT]))
        with self.assertRaises(F.FoundryError):
            F.validate_config(cfg(subscriptions=[SUB, SUB]))

    def test_no_scope_fail_closed(self):
        with self.assertRaises(F.FoundryError):
            F.collect(cfg(), client=FakeClient([]), credential=Credential())

    def test_account_list_deployments_get_only_and_entra_management_scope(self):
        client = FakeClient([(200, {"value": [arm()]})])
        credential = Credential()
        results = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=client, credential=credential)
        self.assertEqual("success", results[0]["status"])
        self.assertEqual([F.ARM_SCOPE], credential.scopes)
        self.assertEqual("GET", client.calls[0][0]); self.assertFalse(client.calls[0][3])
        self.assertEqual("Bearer FAKE_TOKEN", client.calls[0][2]["Authorization"])
        record = results[0]["deployments"][0]
        self.assertEqual("gpt-4.1", record["model_name"])
        self.assertEqual("2025-04-14", record["model_version"])
        self.assertEqual("model_format_proxy", record["publisher_basis"])
        self.assertIsNone(record["resource_region"])
        self.assertEqual("NoAutoUpgrade", record["version_upgrade_option"])

    def test_project_list_distinct_scope_and_version(self):
        client = FakeClient([(200, {"value": [proj()]})]); auth = Credential()
        output = F.collect(cfg(projects=[PROJECT]), client=client, credential=auth)
        self.assertEqual([F.PROJECT_SCOPE], auth.scopes)
        self.assertIn("?api-version=v1", client.calls[0][1])
        node = output[0]["deployments"][0]
        self.assertEqual("foundry_project", node["source_kind"])
        self.assertEqual("model_publisher", node["publisher_basis"])
        self.assertIsNone(node["resource_region"])

    def test_subscription_enumerates_accounts_and_preserves_resource_region(self):
        client = FakeClient([(200, {"value": [{"id": ARM_ACCOUNT, "location": "canadacentral"}]}),
                             (200, {"value": [arm()]})])
        out = F.collect(cfg(subscriptions=[SUB]), client=client, credential=Credential())
        self.assertEqual("success", out[0]["status"])
        self.assertEqual("canadacentral", out[0]["deployments"][0]["resource_region"])
        self.assertEqual(2, len(client.calls))

    def test_pagination_keeps_fixed_host_path_and_version(self):
        base = f"https://management.azure.com{ARM_ACCOUNT}/deployments"
        next_url = f"{base}?api-version={F.ARM_API_VERSION}&skiptoken=abc"
        client = FakeClient([(200, {"value": [arm()], "nextLink": next_url}),
                             (200, {"value": [arm("deployment-2")]})])
        result = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=client, credential=Credential())
        self.assertEqual(2, len(result[0]["deployments"]))
        self.assertIn("skiptoken=abc", client.calls[1][1])

    def test_pagination_rejects_token_exfiltration_and_retains_prior(self):
        bad = "https://attacker.example/accounts?api-version=2025-06-01"
        client = FakeClient([(200, {"value": [], "nextLink": bad})])
        result = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=client, credential=Credential())
        self.assertEqual("unavailable", result[0]["status"])
        self.assertEqual(1, len(client.calls))
        self.assertIn("Unsafe pagination", result[0]["message"])
        with self.assertRaises(F.FoundryError):
            F._validate_page_url(f"https://management.azure.com{ARM_ACCOUNT}/deployments?api-version=evil", f"https://management.azure.com{ARM_ACCOUNT}/deployments", F.ARM_API_VERSION)

    def test_partial_subscription_never_marks_missing_deployments(self):
        first = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=FakeClient([(200, {"value": [arm()]})]), credential=Credential())
        state, _ = F.merge(F.empty_state(), first)
        sid = first[0]["source_id"]
        # Simulate partial coverage for an existing source. Do not declare deployment absent.
        updated, _ = F.merge(state, [{"source_id": sid, "status": "partial", "at": "2026-11-17T00:00:00Z", "deployments": []}])
        self.assertEqual("observed", next(iter(updated["deployments"].values()))["observation_status"])
        self.assertEqual(1, F.summary(updated)["partial_sources"])

    def test_failed_source_preserves_old_evidence(self):
        first = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=FakeClient([(200, {"value": [arm()]})]), credential=Credential())
        state, _ = F.merge(F.empty_state(), first)
        bad = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=FakeClient([(403, {})]), credential=Credential())
        updated, events = F.merge(state, bad)
        self.assertEqual(state["deployments"], updated["deployments"])
        self.assertIn("SOURCE_UNAVAILABLE", [e["type"] for e in events])

    def test_successful_disappearance_is_not_deletion(self):
        first = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=FakeClient([(200, {"value": [arm()]})]), credential=Credential())
        state, _ = F.merge(F.empty_state(), first)
        later = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=FakeClient([(200, {"value": []})]), credential=Credential())
        updated, events = F.merge(state, later)
        self.assertEqual("not_observed_in_latest_complete_scan", next(iter(updated["deployments"].values()))["observation_status"])
        self.assertTrue(any(e["type"] == "NOT_OBSERVED_ON_RESCAN" for e in events))
        self.assertEqual(1, len(updated["deployments"]))

    def test_version_change_retains_history_and_trigger(self):
        x = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=FakeClient([(200, {"value": [arm()]})]), credential=Credential())
        state, _ = F.merge(F.empty_state(), x)
        y = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=FakeClient([(200, {"value": [arm(version="2026-01-01")]})]), credential=Credential())
        updated, changes = F.merge(state, y)
        self.assertTrue(any(e["type"] == "DEPLOYMENT_CHANGED" for e in changes))
        self.assertEqual("2025-04-14", next(iter(updated["deployments"].values()))["history"][0]["model_version"])
        self.assertEqual("2026-01-01", next(iter(updated["deployments"].values()))["model_version"])
        self.assertEqual(0, state["deployments"][next(iter(state["deployments"]))]["history"].__len__())

    def test_repeat_identical_observation_is_idempotent(self):
        x = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=FakeClient([(200, {"value": [arm()]})]), credential=Credential(), at="2026-11-17T00:00:00Z")
        state, _ = F.merge(F.empty_state(), x)
        y = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=FakeClient([(200, {"value": [arm()]})]), credential=Credential(), at="2026-11-18T00:00:00Z")
        new, events = F.merge(state, y)
        self.assertEqual([], events)
        self.assertEqual(state["state_version"], new["state_version"])
        self.assertEqual("2026-11-18T00:00:00Z", next(iter(new["deployments"].values()))["last_observed_at"])

    def test_scope_spoofing_rejected_without_persisting(self):
        wrong = arm(); wrong["id"] = wrong["id"].replace("foundrybank", "different")
        out = F.collect(cfg(accounts=[ARM_ACCOUNT]), client=FakeClient([(200, {"value": [wrong]})]), credential=Credential())
        self.assertEqual("unavailable", out[0]["status"])
        self.assertEqual([], out[0]["deployments"])

    def test_atomic_state_and_excel_formula_injection(self):
        from openpyxl import Workbook, load_workbook
        r = F.collect(cfg(projects=[PROJECT]), client=FakeClient([(200, {"value": [proj(name='=HYPERLINK("https://evil", "click")')]})]), credential=Credential())
        state, _ = F.merge(F.empty_state(), r)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / "test.xlsx"
            book = Workbook(); book.active.title = "Model Register"; book.active["A1"] = "unchanged"; book.save(path)
            F.add_excel_sheets(path, state)
            check = load_workbook(path)
            self.assertEqual("unchanged", check["Model Register"]["A1"].value)
            self.assertEqual("s", check["Foundry Deployments"]["D2"].data_type)
            check.close()
            F.save_state(root / "foundry.json", state)
            self.assertEqual(state, F.load_state(root / "foundry.json"))
            F.export_json(state, root)
            self.assertEqual(4, len(list(root.glob("foundry_*.json"))))

    def test_failed_acquisition_does_not_expose_token_string(self):
        class Failing:
            def get_token(self, scope):
                raise RuntimeError("client-secret=SENSITIVE-CREDENTIAL")
        result = F.collect(cfg(projects=[PROJECT]), client=FakeClient([]), credential=Failing())
        self.assertEqual("unavailable", result[0]["status"])
        self.assertNotIn("SENSITIVE-CREDENTIAL", str(result))


class FoundryIdentity(unittest.TestCase):
    def _nodes(self, *, model="gpt-4.1", version="2025-04-14", publisher="OpenAI"):
        deployed = F.collect(cfg(projects=[PROJECT]), client=FakeClient([(200, {"value": [proj(model=model, version=version, publisher=publisher)]})]), credential=Credential())
        state, _ = F.merge(F.empty_state(), deployed)
        register = {"records": [{"record_id": "record-a", "deployment_key": "unassigned",
            "vendor": {"Provider": "OpenAI", "Exact Model ID": "gpt-4.1", "Model Family": "GPT-4.1", "Version": "2025-04-14"},
            "organization": {"Organization Classification": "Restricted"}}]}
        return D.node_inventory(I.empty_state(), register, state), state

    def test_foundry_to_register_is_review_only_hosting_suggestion(self):
        nodes, state = self._nodes()
        hints = D.suggestions(nodes, D.empty_state())
        self.assertEqual(1, len(hints))
        self.assertEqual("foundry_model_name_and_version", hints[0]["match_rule"])
        self.assertEqual("hosted_offering_of", hints[0]["proposed_relation"])
        self.assertEqual("unreviewed", hints[0]["link_status"])
        self.assertIn("no runtime checkpoint attestation", hints[0]["reason"])

    def test_conflicting_declared_version_excludes_match(self):
        nodes, _ = self._nodes(version="2026-08-01")
        self.assertEqual([], D.suggestions(nodes, D.empty_state()))

    def test_cross_developer_never_suggests(self):
        nodes, _ = self._nodes(publisher="Meta")
        self.assertEqual([], D.suggestions(nodes, D.empty_state()))

    def test_confirm_requires_reference_and_does_not_change_bank_decision(self):
        nodes, foundry_state = self._nodes()
        original = copy.deepcopy(nodes["register:record-a"])
        queue = D.review_queue(nodes, D.empty_state())
        self.assertEqual(1, len(queue["items"]))
        queue["items"][0].update(action="confirm", relation="hosted_offering_of",
            evidence_url="https://learn.microsoft.com/en-us/azure/foundry/reference/rest/deployments-list",
            review_note="Deployment configuration reviewed against current source evidence")
        confirmed = D.apply_review(D.empty_state(), nodes, queue, "Security reviewer")
        self.assertEqual("confirmed", next(iter(confirmed["links"].values()))["status"])
        self.assertEqual(original, nodes["register:record-a"])
        key = next(iter(foundry_state["deployments"]))
        changed = copy.deepcopy(foundry_state)
        changed["deployments"][key]["observation_status"] = "not_observed_in_latest_complete_scan"
        altered = D.node_inventory(I.empty_state(), {"records": [{"record_id": "record-a", "deployment_key": "unassigned",
            "vendor": {"Provider": "OpenAI", "Exact Model ID": "gpt-4.1", "Model Family": "GPT-4.1", "Version": "2025-04-14"}}]}, changed)
        link = next(iter(confirmed["links"].values()))
        self.assertEqual("stale_deployment_observation", D.link_status(link, altered))

    def test_screening_is_not_approval_or_automatic_policy_compliance(self):
        nodes, foundry_state = self._nodes()
        register = {"records": [{"record_id": "record-a", "vendor": {},
                     "organization": {"Organization Classification": "Restricted"}}]}
        rows = F.screening(foundry_state, register, D.empty_state(), nodes)
        self.assertEqual("Unconfirmed identity suggestion; review", rows[0]["screening_status"])
        q = D.review_queue(nodes, D.empty_state())
        q["items"][0].update(action="confirm", relation="hosted_offering_of",
            evidence_url="https://learn.microsoft.com/en-us/azure/foundry/reference/rest/deployments-list",
            review_note="Reviewed current Azure model deployment and source identity")
        links = D.apply_review(D.empty_state(), nodes, q, "Reviewer")
        rows = F.screening(foundry_state, register, links, nodes)
        self.assertEqual("Potential restricted-model exposure; investigate", rows[0]["screening_status"])
        self.assertNotIn("Compliant", str(rows))

    def test_absent_foundry_source_does_not_change_prior_identity_suggestions(self):
        nodes = D.node_inventory(I.empty_state(), {"records": []})
        self.assertEqual([], D.suggestions(nodes, D.empty_state()))


if __name__ == "__main__":
    unittest.main()
