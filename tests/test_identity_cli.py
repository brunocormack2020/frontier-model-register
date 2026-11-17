"""End-to-end offline identity CLI workflow: requires the bundled repository seed."""
from __future__ import annotations
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from frontier_register import intelligence as I
from frontier_register import identity as D


class IdentityCliTests(unittest.TestCase):
    def test_cli_suggest_confirm_export_does_not_mutate_register(self):
        # The integration check runs in the complete repository (not a minimal patch folder).
        workbook=ROOT/"data"/"Organization_Frontier_Model_Register_2026-11-14.xlsx"
        if not workbook.exists():
            self.skipTest("Repository seed unavailable in a standalone patch test folder")
        import yaml
        from frontier_register.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg=yaml.safe_load((ROOT/"config.example.yaml").read_text(encoding="utf-8"))
            cfg["work_dir"]="work";cfg["output_dir"]="out"
            cfg["benchmarks"]["enabled"]=False
            cfg["intelligence"]={"enabled":True,"apis":{"mistral":{"enabled":False},"xai":{"enabled":False}},
                                 "huggingface":{"enabled":False,"repositories":[],"publishers":[]}}
            (root/"config.yaml").write_text(yaml.safe_dump(cfg),encoding="utf-8")
            config=str(root/"config.yaml")
            self.assertEqual(0,main(["--config",config,"init","--workbook",str(workbook)]))
            before=(root/"work"/"register.json").read_bytes()
            left=I._candidate(key="api:Mistral:mistral-small-test-1",source_type="developer_api",publisher="Mistral",
                              name="mistral-small-test-1",url="https://api.mistral.ai/v1/models",
                              details={"api_model_id":"mistral-small-test-1","aliases":[]},retrieved_at="2026-11-16T00:00:00Z")
            right=I._candidate(key="hf:mistralai/Mistral-Small-Test-1",source_type="huggingface_repository",publisher="mistralai",
                               name="mistralai/Mistral-Small-Test-1",url="https://huggingface.co/mistralai/Mistral-Small-Test-1",
                               details={"repository_id":"mistralai/Mistral-Small-Test-1","revision_sha":"a"*40},
                               retrieved_at="2026-11-16T00:00:00Z")
            intel,_=I.merge(I.empty_state(),[{"source_id":"api:mistral","status":"success","at":"2026-11-16T00:00:00Z","candidates":[left]},
                                               {"source_id":"hf:repo:mistralai/Mistral-Small-Test-1","status":"success",
                                                "at":"2026-11-16T00:00:00Z","candidates":[right]}])
            for c in intel["candidates"].values():c["review"]["status"]="acknowledged"
            I.save_state(root/"work"/"intelligence.json",intel)
            self.assertEqual(0,main(["--config",config,"identity-status"]))
            self.assertEqual(0,main(["--config",config,"identity-suggest"]))
            q=json.loads((root/"out"/"identity_review_queue.json").read_text())
            self.assertEqual(1,len(q["items"]))
            q["items"][0].update(action="confirm",relation="same_model_version",
                                 evidence_url="https://docs.mistral.ai/models/",
                                 review_note="Independent review of named versions and official documentation")
            (root/"work"/"identity_working.json").write_text(json.dumps(q),encoding="utf-8")
            self.assertEqual(0,main(["--config",config,"identity-review","--file",str(root/"work"/"identity_working.json"),
                                     "--reviewer","Model Security Reviewer"]))
            self.assertEqual(0,main(["--config",config,"identity-export"]))
            self.assertEqual(before,(root/"work"/"register.json").read_bytes())
            saved=D.load_state(root/"work"/"identity.json")
            self.assertEqual(1,len(saved["links"]))
            self.assertTrue(all(link["status"]=="confirmed" for link in saved["links"].values()))
            from openpyxl import load_workbook
            wb=load_workbook(root/"out"/"model_register.xlsx",read_only=True)
            self.assertIn("Model Identity Links",wb.sheetnames)
            self.assertIn("Identity Suggestions",wb.sheetnames)
            self.assertEqual("confirmed",wb["Model Identity Links"]["E2"].value)
            wb.close()


if __name__=="__main__":unittest.main()
