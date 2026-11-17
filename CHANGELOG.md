# Changelog

## 1.3.0 — 16 November 2026

### Evidence-bound model identity reconciliation

- Added a separate reviewed identity relationship store (`work/identity.json`) and deterministic name-based suggestions between Mistral API offerings and curated Mistral Hugging Face repositories.
- Added `identity-status`, `identity-suggest`, `identity-template`, `identity-review`, and `identity-export` commands; manual linking supports any two observed candidates or existing register records.
- Introduced typed pair relationships (`same_family`, `same_model_version`, `hosted_offering_of`, `exact_checkpoint`), review attribution, substantive evidence requirements, explicit negative/revocation decisions and bounded audit/history.
- Linked decisions are bound to both source evidence digests. Changes invalidate effective linkage and reopen review; no transitive linking, model approval or benchmark propagation.
- Added two Excel worksheets plus identity JSON, source/review integrity tests, and an offline end-to-end CLI regression test.
- This is additive to v1.2.0 intelligence and existing organization state; no initialization or evidence reimport required.

## 1.2.0 — 16 November 2026

### Vendor and third-party model intelligence — discovery phase

- Added read-only Mistral model-list and xAI language-model API discovery (credentials via environment, opt-in).
- Added curated Hugging Face publisher watches and exact repository metadata snapshots (revision, declared license, gating, file-type metadata; no weights or executable code).
- Added independent `work/intelligence.json` state, candidate review queue, source health, new/change events and three Excel worksheets.
- Added `intelligence-refresh`, `intelligence-status`, `intelligence-review`, `intelligence-export` commands; no changes to governed model approvals.
- Explicitly preserves prior observations on collection failures and marks capped publisher listings as partial, never authoritative completeness evidence.
- Added offline regression tests, developer-intelligence documentation and GitHub test workflow.


## 1.1.1 — 15 November 2026

### Benchmark Coverage score display

- Fixed the coverage worksheet's count-only display. Cells now preview published values, metrics and test settings for linked results and clearly labeled unconfirmed name/version candidates.
- Added internal links to the first displayed `Benchmark Results` or `AISI Evidence` row. Longer evidence lists are explicitly marked as a preview, with all observations retained in the detail sheets.
- Preserved separate metrics, units, configurations, approximate-count qualifiers and AISI scenarios. No averaging, best-score selection or inferred denominators are used.
- Kept pending/rejected review boundaries, source-change warnings and scope selection intact. Retained coding/SOC results can be viewed with a scope-off reference label.
- Left model mapping policy, evidence, organization decisions and operational state unchanged. Existing v1.1.0 users update the code and run `export`; no reinitialization, seed reimport or dependency update is required.

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
- Added a benchmark review queue, observation history and benchmark evidence hashes for organization-decision confirmation. Accepted evidence and scope changes can trigger reassessment without changing organization decisions.
- Kept the JSON state authoritative. Collection retrieves published evidence; the package does not call an LLM or execute benchmark workloads.

### Upgrade and validation

Existing v1 users should back up their operational state, update the configuration and dependencies, import both bundled evidence JSON files, and export again. **Do not reinitialize an existing register.** See the README's upgrade instructions. Generate new organization-decision templates before confirming decisions because v1 templates lack `expected_benchmark_sha256`.

The release's public-source snapshots were collected using `curl` and validated with the parsers. A missing optional SOCKS dependency prevented an end-to-end HTTPX network check in the development environment. Company proxy, source-access and Confluence checks remain necessary; this release is not a blanket production-readiness certification.
