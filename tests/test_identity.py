"""Offline invariants for evidence-bound, non-transitive model identity links."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from frontier_register import identity as D
from frontier_register import intelligence as I


def intel_candidate(key,model,publisher,source_type,details=None, *, sha=None, review="acknowledged"):
    data={"candidate_id":key,"model":model,"publisher":publisher,"source_type":source_type,
          "details":details or {},"source_url":"https://example.org/evidence", "content_sha256":sha or "a"*64,
          "review":{"status":review},"retrieved_at":"2026-11-15T00:00:00Z"}
    return data


def fixture(*, pending=False, register=False):
    left="api:Mistral:mistral-small-3.1-24b-instruct-2503"
    right="hf:mistralai/Mistral-Small-3.1-24B-Instruct-2503"
    candidates={
        left:intel_candidate(left,"mistral-small-3.1-24b-instruct-2503","Mistral","developer_api",
                              {"api_model_id":"mistral-small-3.1-24b-instruct-2503","aliases":["mistral-small-latest"],"developer_identity_confirmed":False},review="pending" if pending else "acknowledged"),
        right:intel_candidate(right,"mistralai/Mistral-Small-3.1-24B-Instruct-2503","mistralai","huggingface_repository",
                              {"repository_id":"mistralai/Mistral-Small-3.1-24B-Instruct-2503", "revision_sha":"b"*40},sha="b"*64),
    }
    registry=[]
    if register:
        registry=[{"record_id":"register-item-1", "deployment_key":"unassigned",
                   "vendor":{"Provider":"OpenAI","Exact Model ID":"example-2026","Model Family":"Example","Version":"2026"},
                   "organization":{"Organization Classification":"Restricted"}}]
    return D.node_inventory({"candidates":candidates},{"records":registry}),left,right


def first_request(nodes, state=None):
    state=state or D.empty_state()
    q=D.review_queue(nodes,state)
    assert len(q["items"])==1
    return q


def fill(q,relation="same_model_version",action="confirm"):
    q=copy.deepcopy(q)
    row=q["items"][0]
    row.update(action=action,relation=relation,evidence_url="https://docs.mistral.ai/models/",
               review_note="Reviewed source documentation and model identifiers; record a limited relationship.")
    return q


class IdentityTests(unittest.TestCase):
    def test_only_recommendations_no_automatic_merges(self):
        nodes,left,right=fixture();state=D.empty_state()
        q=first_request(nodes,state)
        self.assertEqual(0,len(state["links"]))
        self.assertIn("checkpoint NOT verified",q["items"][0]["reason"])
        self.assertEqual({f"intel:{left}",f"intel:{right}"},set([q["items"][0]["left_id"],q["items"][0]["right_id"]]))
        self.assertEqual("unreviewed",D.suggestions(nodes,state)[0]["link_status"])

    def test_confirmation_requires_attributed_review_and_https_proof(self):
        nodes,_,_=fixture();state=D.empty_state()
        bad=fill(first_request(nodes))
        bad["items"][0]["evidence_url"]="http://example.com"
        with self.assertRaises(D.IntelligenceError):D.apply_review(state,nodes,bad,"Reviewer")
        with self.assertRaises(D.IntelligenceError):D.apply_review(state,nodes,fill(first_request(nodes))," ")
        accepted=D.apply_review(state,nodes,fill(first_request(nodes)),"Reviewer",at="2026-11-16T01:00:00Z")
        link=next(iter(accepted["links"].values()))
        self.assertEqual("confirmed",D.link_status(link,nodes))
        self.assertEqual("same_model_version",link["relation"])
        self.assertEqual("Reviewer",link["reviewer"])
        self.assertEqual(0,len(state["links"]))
        self.assertEqual([],D.review_queue(nodes,accepted)["items"])

    def test_exact_checkpoint_requires_stronger_specific_evidence(self):
        nodes,_,_=fixture()
        req=fill(first_request(nodes),relation="exact_checkpoint")
        with self.assertRaises(D.IntelligenceError):D.apply_review(D.empty_state(),nodes,req,"Reviewer")
        req["items"][0].update(proof_kind="provider_statement",proof_reference="Vendor maps API model to this exact checkpoint revision bbb...")
        confirmed=D.apply_review(D.empty_state(),nodes,req,"Reviewer")
        self.assertEqual("exact_checkpoint",next(iter(confirmed["links"].values()))["relation"])

    def test_pending_or_rejected_metadata_cannot_confirm(self):
        nodes,_,_=fixture(pending=True)
        with self.assertRaises(D.IntelligenceError):D.apply_review(D.empty_state(),nodes,fill(first_request(nodes)),"Reviewer")
        nodes[next(iter(nodes))]["review_status"]="rejected"
        with self.assertRaises(D.IntelligenceError):D.apply_review(D.empty_state(),nodes,fill(first_request(nodes)),"Reviewer")

    def test_stale_queue_and_stale_link_on_observed_revision_change(self):
        nodes,_,_=fixture();state=D.empty_state()
        q=fill(first_request(nodes))
        node=list(nodes)[0]
        changed=copy.deepcopy(nodes);changed[node]["evidence_sha256"]="f"*64
        with self.assertRaises(D.IntelligenceError):D.apply_review(state,changed,q,"Reviewer")
        approved=D.apply_review(state,nodes,q,"Reviewer")
        self.assertEqual("stale_evidence",D.link_status(next(iter(approved["links"].values())),changed))
        self.assertEqual(1,D.summary(approved,changed)["stale_links"])
        self.assertEqual(1,len(D.review_queue(changed,approved)["items"]))
        self.assertEqual("confirmed",D.link_status(next(iter(approved["links"].values())),nodes))

    def test_reviewer_must_reacknowledge_changed_candidate_before_reconfirmation(self):
        nodes,_,_=fixture();state=D.apply_review(D.empty_state(),nodes,fill(first_request(nodes)),"Reviewer")
        modified=copy.deepcopy(nodes);modified[list(nodes)[0]]["review_status"]="pending"
        modified[list(nodes)[0]]["evidence_sha256"]="f"*64
        self.assertEqual("stale_evidence",D.link_status(next(iter(state["links"].values())),modified))
        request=fill(D.review_queue(modified,state))
        with self.assertRaises(D.IntelligenceError):D.apply_review(state,modified,request,"Reviewer")
        modified[list(nodes)[0]]["review_status"]="acknowledged"
        request=fill(D.review_queue(modified,state))
        again=D.apply_review(state,modified,request,"Reviewer")
        self.assertEqual(1,len(next(iter(again["links"].values()))["history"]))

    def test_reject_and_revoke_have_independent_lifecycle(self):
        nodes,_,_=fixture();state=D.empty_state()
        req=fill(first_request(nodes),action="reject")
        bad=D.apply_review(state,nodes,req,"Reviewer")
        self.assertEqual("rejected",D.link_status(next(iter(bad["links"].values())),nodes))
        self.assertEqual([],D.review_queue(nodes,bad)["items"])
        manual=D.manual_template(nodes,bad,*[f"intel:{x}" for x in fixture()[1:]])
        manual["items"][0].update(action="revoke",review_note="Pair withdrawn after security governance review")
        revoked=D.apply_review(bad,nodes,manual,"Reviewer")
        self.assertEqual("revoked",next(iter(revoked["links"].values()))["status"])
        self.assertEqual(1,len(D.review_queue(nodes,revoked)["items"]))

    def test_cannot_change_active_relation_without_revocation(self):
        nodes,_,_=fixture();state=D.apply_review(D.empty_state(),nodes,fill(first_request(nodes)),"Reviewer")
        req=fill(D.manual_template(nodes,state,*[f"intel:{x}" for x in fixture()[1:]]),relation="same_family")
        with self.assertRaises(D.IntelligenceError):D.apply_review(state,nodes,req,"Reviewer")

    def test_forged_pair_duplicate_and_evidence_fail_atomically(self):
        nodes,_,_=fixture();state=D.empty_state();q=fill(first_request(nodes))
        forged=copy.deepcopy(q);forged["items"][0]["pair_id"]="f"*24
        with self.assertRaises(D.IntelligenceError):D.apply_review(state,nodes,forged,"Reviewer")
        duplicated=copy.deepcopy(q);duplicated["items"].append(copy.deepcopy(duplicated["items"][0]))
        with self.assertRaises(D.IntelligenceError):D.apply_review(state,nodes,duplicated,"Reviewer")
        bad=copy.deepcopy(q);bad["items"][0]["evidence_hashes"]={}
        with self.assertRaises(D.IntelligenceError):D.apply_review(state,nodes,bad,"Reviewer")
        self.assertEqual(0,len(state["links"]))

    def test_review_state_change_invalidates_old_snapshot(self):
        nodes,_,_=fixture();state=D.empty_state();q=fill(first_request(nodes))
        new=D.apply_review(state,nodes,q,"Reviewer")
        with self.assertRaises(D.IntelligenceError):D.apply_review(new,nodes,q,"Reviewer")

    def test_manual_registered_link_is_evidence_bound_but_does_not_modify_register(self):
        nodes,left,_=fixture(register=True)
        reg="register:register-item-1"
        saved=copy.deepcopy(nodes[reg])
        req=D.manual_template(nodes,D.empty_state(),f"intel:{left}",reg)
        req=fill(req,relation="hosted_offering_of")
        result=D.apply_review(D.empty_state(),nodes,req,"Identity reviewer")
        self.assertEqual("confirmed",next(iter(result["links"].values()))["status"])
        self.assertEqual(saved,nodes[reg])
        altered=copy.deepcopy(nodes);altered[reg]["evidence_sha256"]="0"*64
        self.assertEqual("stale_evidence",D.link_status(next(iter(result["links"].values())),altered))

    def test_no_spurious_cross_vendor_or_shortened_name_suggestions(self):
        nodes,_,_=fixture()
        nodes["intel:hf:evil/Mistral-Small-3.1-24B-Instruct-2503"]={**nodes[next(k for k in nodes if "hf:" in k)],"node_id":"intel:hf:evil/Mistral-Small-3.1-24B-Instruct-2503","provider":"evil"}
        nodes["intel:api:xAI:grok-5"]={"node_id":"intel:api:xAI:grok-5","source_type":"developer_api","provider":"xAI","label":"grok-5","details":{"api_model_id":"grok-5"},"evidence_sha256":"f"*64,"kind":"intelligence_candidate","review_status":"acknowledged"}
        self.assertEqual(1,len(D.suggestions(nodes,D.empty_state())))

    def test_alias_suggestion_identified_mutable_and_not_checkpoint(self):
        nodes,_,_=fixture()
        hf=next(k for k in nodes if "hf:" in k)
        nodes[hf]["label"]="mistralai/mistral-small-latest"
        nodes[hf]["details"]["repository_id"]="mistralai/mistral-small-latest"
        s=D.suggestions(nodes,D.empty_state())
        self.assertEqual(1,len(s))
        self.assertIn("mutable API alias",s[0]["reason"])

    def test_no_transitive_inference(self):
        nodes,left,right=fixture(register=True)
        base=D.empty_state()
        first=D.apply_review(base,nodes,fill(D.manual_template(nodes,base,f"intel:{left}",f"intel:{right}")),"Reviewer")
        second=D.apply_review(first,nodes,fill(D.manual_template(nodes,first,f"intel:{right}","register:register-item-1")),"Reviewer")
        self.assertEqual(2,len(second["links"]))
        a,b,pair_id=D._pair(f"intel:{left}","register:register-item-1",nodes)
        self.assertNotIn(pair_id,second["links"])

    def test_json_exports_persist_without_mutating_prior_state(self):
        nodes,_,_=fixture();state=D.apply_review(D.empty_state(),nodes,fill(first_request(nodes)),"Reviewer")
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            D.save_state(root/"identity_links.json",state)
            loaded=D.load_state(root/"identity_links.json")
            self.assertEqual(state,loaded)
            D.export_json(loaded,nodes,root)
            self.assertEqual(1,len(json.loads((root/"identity_links.json").read_text())["links"]))
            self.assertEqual(1,len(json.loads((root/"identity_suggestions.json").read_text())["suggestions"]))
            self.assertTrue((root/"identity_review_queue.json").is_file())

    def test_excel_sheets_preserve_existing_and_never_execute_source_values(self):
        from openpyxl import Workbook,load_workbook
        nodes,left,_=fixture()
        nodes[f"intel:{left}"]["label"]='=HYPERLINK("https://bad", "click")'
        state=D.empty_state()
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"register.xlsx";w=Workbook();w.active.title="Model Register";w.active["A1"]="untouched";w.save(p)
            D.add_excel_sheets(p,state,nodes)
            out=load_workbook(p)
            self.assertIn("Identity Suggestions",out.sheetnames)
            self.assertIn("Model Identity Links",out.sheetnames)
            self.assertEqual("untouched",out["Model Register"]["A1"].value)
            self.assertEqual('=HYPERLINK("https://bad", "click")',out["Identity Suggestions"]["B2"].value)
            self.assertEqual("s",out["Identity Suggestions"]["B2"].data_type)
            out.close()


if __name__=="__main__": unittest.main()
