"""Read-only developer API and curated Hugging Face intelligence regressions."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from frontier_register import intelligence as I


class FakeClient:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    @contextmanager
    def stream(self, method, url, headers=None, params=None, timeout=None, follow_redirects=None):
        self.calls.append({"method": method,"url":url,"headers":headers,"params":params,
                           "follow_redirects":follow_redirects})
        if not self.replies:
            raise AssertionError(f"No mocked response prepared: {url}")
        status, body = self.replies.pop(0)
        import json
        raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
        response = SimpleNamespace(status_code=status,headers={"content-type":"application/json"},
                                  iter_bytes=lambda:iter([raw]))
        yield response


def cfg(*,mistral=False,xai=False,hf=False,publishers=None,repos=None):
    return {"enabled":True,"timeout_seconds":3,"max_response_bytes":30000,
            "apis":{"mistral":{"enabled":mistral},"xai":{"enabled":xai}},
            "huggingface":{"enabled":hf,"publishers":publishers or [],"repositories":repos or []}}


def repo(repo_id, *, sha="a"*40, license="apache-2.0", siblings=None):
    return {"id":repo_id,"sha":sha,"lastModified":"2026-11-16T00:00:00Z",
            "cardData":{"license":license,"base_model":"Example upstream"},"gated":False,
            "private":False,"pipeline_tag":"text-generation",
            "siblings":siblings or [{"rfilename":"model-00001.safetensors"},{"rfilename":"config.json"}]}


class CollectTests(unittest.TestCase):
    def test_mistral_filters_account_finetunes_and_keeps_claims_unapproved(self):
        api = {"data":[
            {"id":"mistral-large-4", "created":1770000,"capabilities":{"vision":True},"aliases":["mistral-large-latest"]},
            {"id":"ft:mistral:my-bank", "TYPE":"fine-tuned","job":"123"},
            {"id":"custom-xyz", "type":"fine-tuned"},
        ]}
        client = FakeClient([(200,api)])
        outcome=I.collect(cfg(mistral=True), client=client,environ={"MISTRAL_API_KEY":"TEST_SECRET"},at="2026-11-16T00:00:00Z")
        self.assertEqual(1,len(outcome))
        self.assertEqual("success",outcome[0]["status"])
        self.assertEqual(["mistral-large-4"],[c["model"] for c in outcome[0]["candidates"]])
        self.assertEqual("https://api.mistral.ai/v1/models",client.calls[0]["url"])
        self.assertTrue(client.calls[0]["headers"]["Authorization"].endswith("TEST_SECRET"))
        self.assertFalse(client.calls[0]["follow_redirects"])
        state,events = I.merge(I.empty_state(),outcome)
        self.assertEqual("pending",state["candidates"]["api:Mistral:mistral-large-4"]["review"]["status"])
        self.assertNotIn("TEST_SECRET",str(state))
        self.assertEqual(1,len(events))

    def test_xai_language_models_and_aliases_not_independent_models(self):
        client=FakeClient([(200,{"models":[{"id":"grok-4.7", "version":"4.7", "fingerprint":"fp123",
                                                 "aliases":["grok-latest","grok-4.7"],
                                                 "input_modalities":["text"],"output_modalities":["text"]}]})])
        out=I.collect(cfg(xai=True),client=client,environ={"XAI_API_KEY":"SECRET"},at="2026-11-16T00:00:00Z")
        self.assertEqual("https://api.x.ai/v1/language-models",client.calls[0]["url"])
        self.assertEqual("grok-4.7",out[0]["candidates"][0]["model"])
        self.assertEqual(["grok-4.7", "grok-latest"],out[0]["candidates"][0]["details"]["aliases"])
        self.assertEqual(1,len(out[0]["candidates"]))

    def test_no_credential_fails_source_without_leaking_or_deleting(self):
        previous=I.empty_state()
        sample=I._candidate(key="api:Mistral:prior",source_type="developer_api",publisher="Mistral",
                            name="prior",url="https://api.mistral.ai/v1/models",details={},retrieved_at="2026-11-15T00:00:00Z")
        previous,_=I.merge(previous,[{"source_id":"api:mistral","status":"success","at":"2026-11-15T00:00:00Z","candidates":[sample]}])
        bad=I.collect(cfg(mistral=True), client=FakeClient([]), environ={},at="2026-11-16T00:00:00Z")
        self.assertEqual("unavailable",bad[0]["status"])
        self.assertIn("MISTRAL_API_KEY",bad[0]["message"])
        updated,ev=I.merge(previous,bad)
        self.assertIn("api:Mistral:prior",updated["candidates"])
        self.assertEqual("SOURCE_UNAVAILABLE",ev[0]["type"])
        self.assertEqual("2026-11-15T00:00:00Z",updated["source_checks"]["api:mistral"]["last_success_at"])

    def test_hf_explicit_repo_only_reads_metadata_no_weights(self):
        client=FakeClient([(200,repo("deepseek-ai/DeepSeek-R1"))])
        outcomes=I.collect(cfg(hf=True,repos=["deepseek-ai/DeepSeek-R1"]),client=client,environ={},at="2026-11-16T00:00:00Z")
        item=outcomes[0]["candidates"][0]
        self.assertEqual("huggingface_repository",item["source_type"])
        self.assertEqual("a"*40,item["details"]["revision_sha"])
        self.assertEqual({"json":1,"safetensors":1},item["details"]["file_extensions"])
        self.assertEqual("apache-2.0",item["details"]["declared_license"])
        self.assertNotIn("downloads",item["details"])
        self.assertIn("/api/models/deepseek-ai/DeepSeek-R1",client.calls[0]["url"])
        self.assertEqual("GET",client.calls[0]["method"])
        self.assertNotIn("Authorization",client.calls[0]["headers"])

    def test_hf_publisher_enforces_author_and_prefix_and_flags_limit(self):
        client=FakeClient([(200,[repo("Qwen/Qwen3-32B"),repo("Qwen/Unrelated")])])
        selectors=[{"name":"Qwen","prefixes":["Qwen"],"max_results":2}]
        out=I.collect(cfg(hf=True,publishers=selectors),client=client,environ={})
        self.assertEqual("partial",out[0]["status"])
        self.assertEqual(["Qwen/Qwen3-32B"],[c["model"] for c in out[0]["candidates"]])
        self.assertEqual("Qwen",client.calls[0]["params"]["author"])
        self.assertEqual(2,client.calls[0]["params"]["limit"])

    def test_hf_publisher_author_violation_aborts_batch(self):
        client=FakeClient([(200,[repo("untrusted/Model")])])
        out=I.collect(cfg(hf=True,publishers=[{"name":"meta-llama","prefixes":["Llama"],"max_results":10}]),client=client,environ={})
        self.assertEqual("unavailable",out[0]["status"])
        self.assertEqual([],out[0]["candidates"])

    def test_hf_duplicate_publisher_and_explicit_repo_prefers_detail(self):
        item=repo("mistralai/Mistral-7B-Instruct-v0.3")
        publisher={**item,"siblings":[]}
        client=FakeClient([(200,[publisher]),(200,item)])
        conf=cfg(hf=True,publishers=[{"name":"mistralai","prefixes":["Mistral"],"max_results":4}],
                 repos=["mistralai/Mistral-7B-Instruct-v0.3"])
        out=I.collect(conf,client=client,environ={})
        state,events=I.merge(I.empty_state(),out)
        self.assertEqual(1,len(state["candidates"]))
        self.assertEqual(1,len([e for e in events if e["type"]=="NEW_CANDIDATE"]))
        self.assertEqual(2,state["candidates"]["hf:mistralai/Mistral-7B-Instruct-v0.3"]["details"]["file_count_reported"])

    def test_unavailable_repo_keeps_prior_candidate(self):
        path="meta-llama/Llama-3.3-70B-Instruct"
        c=cfg(hf=True,repos=[path])
        first=I.collect(c,client=FakeClient([(200,repo(path))]),environ={})
        state,_=I.merge(I.empty_state(),first)
        later=I.collect(c,client=FakeClient([(404,{"error":"not found"})]),environ={})
        updated,_=I.merge(state,later)
        self.assertEqual(1,len(updated["candidates"]))
        self.assertEqual("unavailable",updated["source_checks"][f"hf:repo:{path}"]["status"])

    def test_refresh_same_evidence_doesnt_overwrite_reviewer(self):
        c=cfg(hf=True,repos=["Qwen/Qwen3-32B"])
        outcomes=I.collect(c,client=FakeClient([(200,repo("Qwen/Qwen3-32B"))]),environ={})
        state,_=I.merge(I.empty_state(),outcomes)
        request=I.queue(state)
        request["items"][0].update(action="acknowledge",review_note="Publisher metadata inspected; still requires model eligibility decision")
        state=I.apply_review(state,request,"Reviewer",at="2026-11-16T00:00:01Z")
        later=I.collect(c,client=FakeClient([(200,repo("Qwen/Qwen3-32B"))]),environ={},at="2026-11-17T00:00:00Z")
        refreshed,changes=I.merge(state,later)
        self.assertEqual([],changes)
        self.assertEqual("acknowledged",next(iter(refreshed["candidates"].values()))["review"]["status"])
        self.assertEqual([],I.queue(refreshed)["items"])

    def test_sha_change_reopens_review_and_keeps_history(self):
        c=cfg(hf=True,repos=["Qwen/Qwen3-32B"])
        state,_=I.merge(I.empty_state(),I.collect(c,client=FakeClient([(200,repo("Qwen/Qwen3-32B"))]),environ={}))
        q=I.queue(state);q["items"][0].update(action="acknowledge",review_note="Reviewed")
        state=I.apply_review(state,q,"Reviewer")
        changed=I.collect(c,client=FakeClient([(200,repo("Qwen/Qwen3-32B",sha="b"*40))]),environ={})
        new,events=I.merge(state,changed)
        self.assertTrue(any(e["type"]=="METADATA_CHANGED" for e in events))
        entry=next(iter(new["candidates"].values()))
        self.assertEqual("pending",entry["review"]["status"])
        self.assertEqual(1,len(entry["history"]))
        self.assertEqual("a"*40,entry["history"][0]["previous_details"]["revision_sha"])

    def test_stale_review_does_not_mutate(self):
        state=I.empty_state()
        state,_=I.merge(state,[{"source_id":"api:mistral","status":"success","at":"2026-11-16T00:00:00Z",
                                "candidates":[I._candidate(key="api:Mistral:model",source_type="developer_api",
                                     publisher="Mistral",name="model",url=I._MISTRAL_URL,details={},retrieved_at="2026-11-16T00:00:00Z")]}])
        q=I.queue(state);q["items"][0].update(action="acknowledge",review_note="Reviewed")
        stale=copy.deepcopy(q); stale["state_sha256"]="f"*64
        previous=copy.deepcopy(state)
        with self.assertRaises(I.IntelligenceError):
            I.apply_review(state,stale,"Reviewer")
        self.assertEqual(state,previous)
        q["items"][0]["content_sha256"]="0"*64
        with self.assertRaises(I.IntelligenceError):
            I.apply_review(state,q,"Reviewer")

    def test_bad_slug_rejected_before_network(self):
        for bad in ("meta-llama/../admin", "https://evil.com/x", "publisher/", "../x", "publisher/repo?token=123"):
            with self.subTest(bad=bad):
                with self.assertRaises(I.IntelligenceError):
                    I.collect(cfg(hf=True,repos=[bad]),client=FakeClient([]))

    def test_schema_rejects_boolean_as_integer_and_unsupported_config(self):
        for max_results in (-1, True, 0, 1000):
            with self.subTest(max_results=max_results):
                with self.assertRaises(I.IntelligenceError):
                    I.validate_config(cfg(hf=True,publishers=[{"name":"Qwen","prefixes":["Qwen"],"max_results":max_results}]))

    def test_oversized_response_fails_safe(self):
        doc={"models":[{"id":"grok-4.7","junk":"A"*30000}]}
        c=cfg(xai=True)
        out=I.collect(c,client=FakeClient([(200,doc)]),environ={"XAI_API_KEY":"secret"})
        self.assertEqual("unavailable",out[0]["status"])
        self.assertEqual([],out[0]["candidates"])

    def test_outputs_atomic_and_excel_strings_not_formulas(self):
        from openpyxl import Workbook,load_workbook
        item=I._candidate(key="api:Mistral:=HYPERLINK('a','b')",source_type="developer_api",
                           publisher="Mistral",name="=HYPERLINK('a','b')",url=I._MISTRAL_URL,
                           details={"aliases":[]},retrieved_at="2026-11-16T00:00:00Z")
        state,_=I.merge(I.empty_state(),[{"source_id":"api:mistral","status":"success",
                                         "at":"2026-11-16T00:00:00Z","candidates":[item]}])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            I.save_state(root/"intelligence.json",state)
            self.assertEqual(state,I.load_state(root/"intelligence.json"))
            I.export_json(state,root/"out")
            for name in ("intelligence_candidates.json","intelligence_events.json","intelligence_review_queue.json","intelligence_summary.json"):
                self.assertTrue((root/"out"/name).exists())
            wb=Workbook();wb.active.title="Model Register";wb.save(root/"out"/"register.xlsx")
            I.add_excel_sheets(root/"out"/"register.xlsx",state)
            wb=load_workbook(root/"out"/"register.xlsx")
            self.assertIn("Vendor Intelligence",wb.sheetnames)
            self.assertIn("Intelligence Events",wb.sheetnames)
            self.assertIn("Intelligence Sources",wb.sheetnames)
            self.assertEqual("s",wb["Vendor Intelligence"]["C2"].data_type)
            self.assertIn("=HYPERLINK",wb["Vendor Intelligence"]["C2"].value)
            wb.close()


if __name__ == "__main__":
    unittest.main()
