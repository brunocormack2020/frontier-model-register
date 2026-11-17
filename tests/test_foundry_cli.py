"""End-to-end offline Foundry CLI check using the bundled register workbook."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from frontier_register import foundry as F

SUB = "00000000-1111-2222-3333-444444444444"
PROJECT = "https://examplefoundry.services.ai.azure.com/api/projects/security"


class FoundryCliTests(unittest.TestCase):
    def test_foundry_refresh_status_export_preserves_main_register(self):
        workbook = ROOT / "data" / "Organization_Frontier_Model_Register_2026-11-14.xlsx"
        if not workbook.exists():
            self.skipTest("Bundled repository seed unavailable in a standalone patch folder")
        import yaml
        from frontier_register.cli import main
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configuration = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
            configuration["benchmarks"]["enabled"] = False
            configuration["foundry"] = {"enabled": True, "subscriptions": [], "accounts": [],
                                       "projects": [PROJECT], "max_pages": 2}
            (root / "config.yaml").write_text(yaml.safe_dump(configuration), encoding="utf-8")
            cfg = str(root / "config.yaml")
            self.assertEqual(0, main(["--config", cfg, "init", "--workbook", str(workbook)]))
            prior = (root / "work" / "register.json").read_bytes()
            sid = f"project:{PROJECT.lower()}"
            item = F._deployment_project({"name": "security-gpt", "modelName": "gpt-4.1",
                "modelVersion": "2025-04-14", "modelPublisher": "OpenAI", "type": "ModelDeployment",
                "sku": {"name": "Standard"}}, PROJECT, sid)
            response = [{"source_id": sid, "status": "success", "at": "2026-11-17T00:00:00Z",
                         "deployments": [item]}]
            with patch("frontier_register.cli.collect_foundry", return_value=response):
                self.assertEqual(0, main(["--config", cfg, "foundry-refresh"]))
            self.assertEqual(0, main(["--config", cfg, "foundry-status"]))
            self.assertEqual(0, main(["--config", cfg, "identity-suggest"]))
            self.assertEqual(0, main(["--config", cfg, "foundry-export"]))
            self.assertEqual(prior, (root / "work" / "register.json").read_bytes())
            foundry = F.load_state(root / "work" / "foundry.json")
            self.assertEqual(1, len(foundry["deployments"]))
            self.assertEqual(1, json.loads((root / "out" / "foundry_summary.json").read_text())["deployments"])
            from openpyxl import load_workbook
            wb = load_workbook(root / "out" / "model_register.xlsx", read_only=True)
            for sheet in ("Foundry Deployments", "Deployment Intelligence Events", "Foundry Sources",
                          "Model Register", "Identity Suggestions"):
                self.assertIn(sheet, wb.sheetnames)
            wb.close()


if __name__ == "__main__":
    unittest.main()
