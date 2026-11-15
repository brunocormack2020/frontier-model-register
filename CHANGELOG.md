# Changelog

## 1.1.0 — 14 November 2026

### Published cyber benchmark evidence

- Added collectors for CyberGym, ExploitGym, CyberGym-E2E, CyberBinGym, CVE-Bench, Cybench and AgentDojo, restricted to Anthropic, OpenAI and Google results.
- Added conditional Secure Code Instruct/Autocomplete and CyberSOCEval Malware/Threat Intelligence observations, plus separate AISI cyber-range findings. Numeric updates to these curated categories use attributed evidence imports.
- Bundled 257 core benchmark observations, 48 conditional observations and 52 AISI findings. All start pending review; historical observations do not imply complete coverage of current models.
- Preserved reported metric units, actual denominators, budgets, scaffolds, safeguards, target mitigations, source dates and provenance where disclosed. Unknown snapshot IDs and evaluation dates remain unknown.
- Added explicit model-mapping review. Matching published IDs can link automatically; reviewed family/version labels remain marked as snapshot-unconfirmed.

### Workflow and outputs

- Added `benchmark-refresh`, `benchmark-import`, `benchmark-review` and `benchmark-scope` commands. Normal source refresh includes configured core benchmarks unless the run targets selected vendor source IDs without `--benchmarks`.
- Added Benchmark Coverage, Benchmark Results, AISI Evidence and Benchmark Guide workbook sheets, two main-register summary columns, and separate Confluence benchmark sections.
- Added a benchmark review queue, observation history and benchmark evidence hashes for bank-decision confirmation. Accepted evidence and scope changes can trigger reassessment without changing bank decisions.
- Kept the JSON state authoritative. Collection retrieves published evidence; the package does not call an LLM or execute benchmark workloads.

### Upgrade and validation

Existing v1 users should back up their operational state, update the configuration and dependencies, import both bundled evidence JSON files, and export again. **Do not reinitialize an existing register.** See the README's upgrade instructions. Generate new bank-decision templates before confirming decisions because v1 templates lack `expected_benchmark_sha256`.

The release's public-source snapshots were collected using `curl` and validated with the parsers. A missing optional SOCKS dependency prevented an end-to-end HTTPX network check in the development environment. Company proxy, source-access and Confluence checks remain necessary; this release is not a blanket production-readiness certification.
