# Bank frontier model register automation — v1.1.1

## Start here

- **Populated Excel register:** [examples/first_run_model_register.xlsx](examples/first_run_model_register.xlsx). Open **Benchmark Results** for 305 published observations and **AISI Evidence** for 52 separate findings.
- **Browser-readable example:** [examples/first_run_register_preview.html](examples/first_run_register_preview.html). Download and open it locally.
- **Python entry point:** [run_register.py](run_register.py). Installation and refresh instructions follow below.
- **Private GitHub setup:** [docs/GITHUB_SETUP.md](docs/GITHUB_SETUP.md).

Version 1.1.1 fixes the **Benchmark Coverage** worksheet: populated cells now show published values, metrics, test settings and mapping/review status, with links to the detailed evidence. Version 1.1.0 showed counts and status messages only. Regenerate an existing register with `export` after updating the code; do not run `init` again.

The workbook in `data/` is the original initialization input. The populated example in `examples/` contains the added benchmark worksheets. Example results are a dated snapshot and all imported benchmark evidence remains pending review.

This Python package imports the supplied register, monitors official vendor evidence, records changes for review, and produces Excel, JSON and Confluence content from one source of truth. It supports **Anthropic, OpenAI and Google**.

The supplied seed is `data/Bank_Frontier_Model_Register_2026-11-14.xlsx`: 23 current model records, 42 prior/special records and the original 38 fields. The seed is a dated starting point, not a continuously verified catalog. Its claims and review limitations remain intact on import.

Version 1.1 adds seven core cyber benchmarks, conditional secure-coding and SOC tracks, and a separate AISI cyber-range evidence register. The bundled evidence contains **257 core benchmark observations, 48 conditional observations and 52 AISI findings**, collected or transcribed from public primary sources. Every observation starts pending review. Several results concern historical models; this is not complete score coverage of every current registered model.

## What is automated

| Function | Behavior |
|---|---|
| Evidence collection | Downloads configured official HTML/PDF sources, extracts text, keeps raw snapshots and records hashes, URLs and timestamps. |
| Change detection | Checks both document bytes and extracted text. Uses ETag/Last-Modified when available. Never treats a failed request as a model removal. |
| Model discovery | Optional read-only vendor catalog requests. Adds unseen in-scope IDs as pending, and flags changed metadata for an existing ID. Account visibility is not a complete global model inventory. |
| Review queue | Produces a JSON file with affected records, source hashes and evidence paths. A reviewer can retain or update vendor findings. |
| Published benchmark collection | Reads official JSON, CSV, YAML or HTML results for the seven core benchmarks. Saves the reported model, metric, configuration, source and provenance as separate observations. |
| Benchmark review | Separates acceptance of a published result from mapping its model label to a registered model. Exact published IDs can link automatically; labels require an attributed mapping review. |
| Bank decisions | Stored separately. Vendor and benchmark imports/reviews cannot overwrite them. Decisions bind to vendor evidence, deployment settings, observed catalog metadata, reviewed benchmark evidence and selected scope. |
| Excel | Preserves the original 38 fields and supporting tabs. Adds six monitoring/provenance columns, Source Monitor, Benchmark Coverage, Benchmark Results, AISI Evidence and Benchmark Guide. |
| Confluence | Produces storage HTML and a local preview. Can update a marked section of an existing Cloud or Data Center page. Publishing is a dry run unless `--apply` is supplied. |
| Recovery and audit | Atomic state files, local version history, attributed review events, content hashes and an exclusive writer lock. |

**Capability interpretation remains a review step.** The program does not infer High/Critical/CCL/ASL equivalence, classify frontier status from model names, invent missing findings, or approve models. It does not call a generative model, send the register to an LLM, or execute benchmark tasks or exploits. Public-source monitoring and published-score collection need no model API key.

## 1. Install and run the first export

Python 3.11 or newer is required. Run from the extracted package directory. Use your company's approved Python distribution and package repository.

### Windows / PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-tested.txt
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe run_register.py --config config.yaml init --workbook data/Bank_Frontier_Model_Register_2026-11-14.xlsx
```

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-tested.txt
cp config.example.yaml config.yaml
.venv/bin/python run_register.py --config config.yaml init --workbook data/Bank_Frontier_Model_Register_2026-11-14.xlsx
```

Initialization performs no network requests and refuses to overwrite an existing state. With the supplied configuration, it imports the workbook and both bundled benchmark evidence files. It creates:

- `work/register.json`: the authoritative operational register.
- `work/template.xlsx`: the imported Excel template.
- `work/history/`: recoverable JSON state versions.
- `out/model_register.xlsx`: Excel output.
- `out/register_preview.html`: a browser-readable preview.
- `out/confluence_fragment.html`: generated Confluence storage content, without markers.
- `out/confluence_initial_section.html`: initial section markup with markers.
- `out/review_queue.json`: pending vendor reviews.
- `out/benchmark_review_queue.json`: pending score, AISI evidence and model-mapping reviews.
- `out/benchmark_results.json`: the benchmark extension, including original observations, review status, mappings, history and source checks.
- `out/register.json` and `out/run_summary.json`: exported state and execution summary.

The first export explicitly says vendor sources have not been checked by this automation. Bundled benchmark observations retain their supplied source-retrieval timestamps; initialization does not claim to have fetched them again. It does not change the seed's original evidence-review dates or accept the imported benchmark observations.

Paths in `config.yaml` are relative to the configuration file's directory. Place the configuration and `work_dir` on company-managed persistent storage when using a scheduler. Keep a single active writer. The local file lock is intended for one machine; use your scheduler's exclusive-job control or a database/distributed lock for multiple workers.

**After initialization, edit through review and bank-update commands.** Direct changes to an exported Excel file are not silently imported on the next run. This prevents two conflicting sources of truth. To begin with a separately edited workbook, use a new `work_dir` and import it deliberately.

### Upgrade v1.1.0 to v1.1.1

Close the generated workbook in Excel, update the project code, and retain your existing `config.yaml` and complete `work/` directory. The coverage-only patch contains `src/frontier_register/benchmark_views.py`, `src/frontier_register/__init__.py`, and `pyproject.toml`; it does not contain configuration, state, evidence seeds or bank decisions. No dependency change or state migration is required.

From your existing project directory in PowerShell, regenerate the workbook:

```powershell
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml export
Start-Process .\out\model_register.xlsx
```

The export reads your current state. It does not accept evidence, change a model mapping, enable conditional scope or collect new scores. The counts in `status` should be unchanged. See **Read the new outputs** below for the meaning of the score previews.

### Upgrade an existing v1 register

Preserve the operational state and the decisions already recorded. Do **not** run `init` again or replace `work/register.json` with an example export.

1. Pause the scheduled writer and back up your existing configuration and complete `work/` directory, including its template, evidence and history. Keep a copy of the last exported views for comparison.
2. Install the v1.1 code and update its dependencies through your approved package repository. Preserve your existing `work_dir`, `output_dir`, source controls and Confluence settings.
3. Merge the `benchmarks` block from `config.example.yaml` into your company configuration. The defaults select all seven core benchmarks; coding and SOC scope initially remain off.
4. Import the two supplied evidence files once into the existing state, then regenerate the outputs:

```bash
python run_register.py --config config.yaml benchmark-import --file data/benchmark_results_seed.json --actor "v1.1 migration operator"
python run_register.py --config config.yaml benchmark-import --file data/benchmark_evidence_seed.json --actor "v1.1 migration operator"
python run_register.py --config config.yaml export
```

These imports add pending evidence without replacing bank decisions. Review the new coverage, queue and reassessment indicators. If a later file corrects a published result under the same observation identity, its prior content remains in history and the correction requires a fresh review. The seeds are starting evidence, not files to re-import on every scheduled run.

Use `benchmark-scope` to select coding or SOC work after initialization, as described below. Generate fresh bank-decision templates when recording the next decision; older templates lack the new benchmark evidence confirmation. Resume the scheduler after checking the upgraded export in your company environment.

## 2. Collect current evidence

```bash
python run_register.py --config config.yaml refresh
```

The program reads URLs already recorded in the current-model rows plus the configured catalog, system-card and deprecation watches. Prior/special rows remain in every export; set `include_prior_sources: true` to monitor their original URLs too.

With `benchmarks.enabled: true` and `benchmarks.refresh_with_sources: true`, a normal `refresh` also collects the configured core benchmark results. A targeted `refresh --source-id ...` checks only the selected vendor sources unless you explicitly add `--benchmarks`. Use `benchmark-refresh` for a benchmark-only collection run.

```bash
python run_register.py --config config.yaml sources
python run_register.py --config config.yaml refresh --source-id SOURCE_ID_FROM_THE_LIST
python run_register.py --config config.yaml status
```

Source snapshots are saved beneath `work/evidence/`. The manifest separates the latest attempt from the last successful content fetch. An HTTP 304 confirms a cached snapshot without pretending the content was downloaded again. Cached content hashes are checked before reuse.

Each run regenerates the views and queue. Exit codes are:

| Code | Meaning |
|---|---|
| `0` | Command completed successfully. This does not mean every model is approved or every field is complete. |
| `1` | Configuration, validation, state, authentication or other command failure. |
| `2` | Refresh/discovery completed with one or more unavailable sources/providers. Existing findings and successful results were retained. |

HTML/PDF extraction is bounded. Unsupported, scanned or incomplete documents are flagged for raw-document review. The default download limit is 10 MB; change it deliberately if your approved source documents require more. JavaScript-only pages, sign-in walls, changed URLs and access controls may require a different official source or manual evidence. The program does not bypass these conditions.

Raw-only changes can include harmless page layout or PDF metadata changes. They are visible because links and other meaningful content can change without changing extracted text. A reviewer can acknowledge a harmless change with `keep_existing`.

## 3. Optionally discover models through vendor APIs

The three vendor integrations are disabled by default. In `config.yaml`, set the relevant `discovery.PROVIDER.enabled` to `true`, then supply credentials through your secret manager or environment:

| Provider | Environment variable | Scope |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | Account-visible official model catalog. Customer-owned fine-tunes are excluded. |
| Anthropic | `ANTHROPIC_API_KEY` | Account-visible Claude catalog. `ANTHROPIC_API_VERSION` can override the documented default. |
| Google | `GOOGLE_API_KEY` or `GEMINI_API_KEY` | Gemini model catalog. The key is sent in a header. |

```bash
python run_register.py --config config.yaml refresh --discover
python run_register.py --config config.yaml discover --provider Anthropic
```

An explicit `--provider` requests that provider even if its normal scheduled discovery is disabled. It still needs the credential and configured prefix scope.

New records start as `Pending classification`, with `Not determined` frontier status and unreviewed capability evidence. Existing catalog observations are compared without the retrieval timestamp; a changed version or other returned metadata produces a new review requirement. A model disappearing from an API response is **not** classified as retired.

If API access is unavailable, add a known identifier for review:

```bash
python run_register.py --config config.yaml add-model --provider Google --model-id gemini-YOUR-EXACT-ID --display-name "Vendor model name"
```

The prefix filters are scope controls, not security classifications. Adapt them to the inventory you maintain. Provider catalogs do not establish availability in Azure, Bedrock, Vertex or a bank-contracted service, and they do not always expose the actual checkpoint behind an alias. Record those deployment bindings separately.

## 4. Review evidence changes

Copy `out/review_queue.json` to a working review file before the next scheduled refresh. It is bound to the exact current state and source hashes. If the state changes before submission, the program refuses the stale review; generate a new queue and reconcile the review.

For each reviewed item:

- Leave `action: "pending"` for work not completed.
- Set `action: "keep_existing"` when the source was reviewed and the existing findings still apply. Leave `proposed_updates` empty.
- Set `action: "update"` and specify only the vendor fields that should change.
- Include a meaningful `review_note` in either accepted case.
- When extraction is incomplete, inspect the preserved raw document and set `raw_evidence_reviewed: true`. An unavailable download cannot be acknowledged as successfully reviewed by this path.
- Keep the supplied source identifiers and hashes. The program checks them before committing.

Example item content, inside a generated queue:

```json
{
  "action": "update",
  "review_note": "Reviewed the new vendor card. Its conclusion applies to this exact release; deployment mapping remains unresolved.",
  "proposed_updates": {
    "Evidence Basis": "Direct model-specific system card reviewed.",
    "Evidence Gaps / Coverage": "Confirm the serving endpoint-to-assessed-revision binding.",
    "Evidence Reviewed On": "2026-11-14"
  }
}
```

This example is an editing pattern, not a new finding for any seeded model. Enter the actual review date and supported conclusions.

```bash
python run_register.py --config config.yaml apply-review --file reviewed_changes.json --reviewer "Named reviewer or company identity"
```

The review command cannot change provider/model identity, the original suggested treatment or bank-owned fields. It does not automatically refresh evidence-review dates; those dates must reflect the review actually performed. A source change acknowledged as immaterial can retain existing vendor fields. A changed vendor finding leaves a previously recorded bank decision visible with a separate reassessment indicator.

Catalog/system-card index watches not yet associated with a specific model remain visible in the watch table. Their changes may identify models that need manual registration; public pages are not scraped into invented model classifications.

## 5. Collect and review benchmark evidence

### Selected benchmarks and interpretation

One observation means one published metric for one reported model and test configuration. The register keeps sample counts, budgets, agent scaffolds, safeguards, target mitigations, dataset subsets and versions where the source discloses them. It does not combine different metrics into a single cyber score.

| Track | Selection | What the result measures | Comparison boundary |
|---|---|---|---|
| [CyberGym](https://www.cybergym.io/cybergym/) | Core | Vulnerability reproduction | Keep task level, trials, tools and memory setting; reproduction alone does not establish a reliable exploit. |
| [ExploitGym](https://www.cybergym.io/exploitgym/) | Core | Exploit generation and intended-vulnerability success | Preserve evaluated subset and mitigation setting; flag capture and intended-vulnerability success are different results. |
| [CyberGym-E2E](https://www.cybergym.io/cybergym-e2e/) | Core | Discovery, repair and staged end-to-end success | Preserve S1–S4 and patch-only distinctions, task-set size and budget. |
| [CyberBinGym](https://www.cybergym.io/cyberbingym/) | Core | Binary vulnerability recovery and exploitation | Keep recovery and exploitation separate, with target hardening, supplied artifacts and safeguard setting. |
| [CVE-Bench](https://cvebench.com/) | Core | Exploitation of vulnerable web applications | The description-withheld/provided information setting, task version and evaluated denominator matter. A historical CVE test is not a newly discovered real-world zero-day. |
| [Cybench](https://cybench.github.io/) | Core | CTF and subtask-solving capability | Preserve guided/unguided settings, subsets, retry policies and published corrections. |
| [AgentDojo](https://agentdojo.spylab.ai/results/) | Core | Prompt-injection attack success and task utility | Lower attack success and higher utility are preferable. Attacks, defenses and agent settings must match before comparing. |
| [Secure Code](https://meta-llama.github.io/PurpleLlama/CyberSecEval/docs/benchmarks/secure_code_generation) — Instruct / Autocomplete | Conditional: coding | Detected vulnerabilities in generated code | The two tracks stay separate. A detector finding no vulnerability is not proof of secure or functionally correct code. |
| [CyberSOCEval](https://arxiv.org/html/2509.20166v1) — Malware / Threat Intelligence | Conditional: SOC | Exact-answer accuracy and, where reported, Jaccard similarity | Keep task, input modality, denominator and implementation distinct; this does not validate an entire SOC workflow. |
| [AISI cyber ranges](https://www.aisi.gov.uk/blog/how-do-frontier-ai-agents-perform-in-multi-step-cyber-attack-scenarios) | Separate evidence | Scenario-specific mean/max progress and full-chain success | Mean progress, maximum progress and full completion are different metrics. Retain run counts, token budgets and absence of active defenders. |

Published benchmark success is **not a probability of breaching the bank**. It describes performance under the reported evaluation conditions. The deployment's permissions, data, tools, human checks, monitoring and safeguards need their own assessment. Greater offensive capability, improved defensive performance and improved prompt-injection resistance are separate properties; a higher number has different meanings across these tracks.

Publication or leaderboard dates, actual evaluation dates and retrieval timestamps are separate fields. A new leaderboard entry may describe an older evaluation; a retrieval today does not make the experiment current. Unknown dates and exact model snapshots remain empty rather than being inferred.

### Configuration and scope

The supplied configuration contains:

```yaml
benchmarks:
  enabled: true
  refresh_with_sources: true
  core: [cybergym, exploitgym, cybergym_e2e, cyberbingym, cve_bench, cybench, agentdojo]
  coding_in_scope: false
  soc_in_scope: false
  seed_files:
    - data/benchmark_results_seed.json
    - data/benchmark_evidence_seed.json
  max_cve_runs: 200
```

`coding_in_scope` and `soc_in_scope` establish the initial state only. Editing them in YAML does not change the scope of an initialized register. Change the stored selection through the attributed command:

```bash
python run_register.py --config config.yaml benchmark-scope --coding on --soc off --actor "Register owner"
python run_register.py --config config.yaml benchmark-scope --soc on --actor "Register owner"
```

You can pass either option independently; the other retains its current value. Turning a track off retains its observations and review history. Conditional observations remain visible in Excel with their scope status, while only selected tracks contribute to active model evidence and the generated Confluence benchmark sections. A scope change changes the decision evidence binding and can require reassessment.

### Refresh published results

```bash
python run_register.py --config config.yaml benchmark-refresh
```

The collectors read the benchmark maintainers' public data representations, not search snippets or generated model assessments. Source snapshots are retained beneath `work/benchmark_evidence/`. The core collectors support the currently published JSON, CSV, YAML and HTML formats. The CVE-Bench collector checks the repository index and matching run metadata; an incomplete or changed index does not silently become a partial successful refresh.

Refreshes retain existing evidence when a source is unavailable or its format cannot be parsed. They flag collection errors and return exit code 2 when a refresh is only partly successful. A removed leaderboard row is not silently interpreted as a model retirement or a zero score. A changed published observation becomes pending review again.

The initial bundle was built from live public snapshots and validated against those source formats. Public-source URLs and schemas can change; this is not a guarantee of indefinite collector compatibility.

### Import conditional results and AISI findings

Secure Code, CyberSOCEval and AISI numeric findings are curated imports. The package monitors their source documents for changes; a changed document is a prompt to inspect and import an attributed update, not an automatic extraction of a new score from prose or a chart.

```bash
python run_register.py --config config.yaml benchmark-import --file published_evidence_update.json --actor "Evidence analyst"
```

Use the bundled JSON files as schema examples. An import has `schema_version: 1` and an `observations` list, an `aisi_findings` list, or both. Keep the source URL, stable `source_result_id`, reported model, metric, value and unit, plus the actual source hash/revision and evaluation conditions when known. Preserve unknown values as `null`. For a correction to an existing result, preserve its identity; for a genuinely different evaluation setting or study, use a distinct identity. Imports and reviews are separate actions.

The conditional seed includes small, cost-bounded Inspect Evals validation subsets: 45 samples for each secure-code track, 14 for malware analysis and 13 for threat intelligence. Those scores are labeled as an adaptation-validation report, not a reproduction of the full original benchmark. The original CyberSOCEval threat-intelligence figures also contain a denominator discrepancy against the paper text; the seed preserves and flags it. Review those qualifications before using the results in a decision. [Inspect report](https://github.com/UKGovernmentBEIS/inspect_evals/blob/main/src/inspect_evals/cyberseceval_4/README.md), [original paper](https://arxiv.org/html/2509.20166v1).

AISI records preserve separate studies and any published corrections. For example, the GPT-5.5 TLO result is explicitly marked as a correction after manual adjudication of an evaluation grading bug. The seed does not reclassify simulated model behavior in other AISI experiments as execution-based cyber-range success. [AISI GPT-5.5 assessment](https://www.aisi.gov.uk/blog/our-evaluation-of-openais-gpt-5-5-cyber-capabilities).

### Review a result and map its model

Copy `out/benchmark_review_queue.json` to a working file before another state-changing run. It is bound to both the register state and the exact observation content. Preserve those hashes and the supplied `published_result` object.

- Leave `action: "pending"` for unfinished work, or use `"accept"` / `"reject"` with a substantive `review_note`.
- An exact published `model_id` links automatically only when it matches one registered model of the same provider. This is a link, not acceptance of the evidence or proof that a deployed alias serves that checkpoint.
- For a name-only result, set the intended `record_id` and explain the correspondence in `mapping_basis`. Suggested name/version matches are aids to review, not automatic joins. The displayed status remains **Reviewed label; snapshot unconfirmed**.
- You may accept an observation without assigning a `record_id`. It remains available as evidence but does not affect a particular model's decision binding.
- Do not change the published value inside a review file. A supported correction belongs in a new attributed evidence import, preserving its provenance.
- When a supplementary source has changed, inspect the current raw source, retain the queue's `source_sha256`, and set `raw_evidence_reviewed: true` before accepting the existing observation again. Explain why the score remains supported, or import the corrected result instead. An unavailable source cannot be acknowledged as successfully inspected.

Example edits to an item in a generated queue:

```json
{
  "action": "accept",
  "record_id": "EXISTING_SAME_PROVIDER_RECORD_UUID",
  "mapping_basis": "The source labels this family and version; reviewed correspondence to this inventory entry. The exact serving snapshot remains unconfirmed.",
  "review_note": "Checked the source, denominator, scaffold, budget and model safeguards. Retained the reported limitations."
}
```

The example shows fields to edit, not a replacement for the full generated item. Use the actual review basis and retain its observation identity, hashes and published data.

```bash
python run_register.py --config config.yaml benchmark-review --file reviewed_benchmark_results.json --reviewer "Named reviewer or company identity"
```

Stale review files are rejected. Accepted results do not change bank fields. A changed accepted evidence set or conditional scope can mark an earlier bank decision for reassessment; it never silently changes the bank's classification.

### Read the new outputs

| Output | Purpose |
|---|---|
| `Benchmark Coverage` | One register entry per row, with published score previews, metrics, test settings, mapping/review status and links to detailed benchmark/AISI evidence. |
| `Benchmark Results` | One metric/configuration per row, with numerical score, unit, denominator, conditions, provenance and review/mapping status. |
| `AISI Evidence` | Scenario-specific progress and success findings, kept outside ordinary benchmark score tables. |
| `Benchmark Guide` | Definitions, metric direction, selected scope, comparison limits and source links. |
| Main register's `Benchmark Coverage` / `Benchmark Evidence Review` columns | Compact summaries that point to the detailed evidence and required review. |

Each populated Coverage cell shows up to three observations in a deterministic preview, keeping each metric and configuration separate. When more are available, the cell states how many are shown. Click the cell to open its first displayed detail row; use the reported model and benchmark filters on the detail sheet to inspect all observations. The preview is not a maximum, average, ranking or combined cyber score. Percentage, fraction and count units remain distinct; approximate published counts retain that qualifier. Numeric source values remain sortable in `Benchmark Results` and `AISI Evidence`.

**Exact-ID links and unconfirmed name/version suggestions are labeled separately.** Showing a candidate's published value does not assign it to the registered model or accept it as bank evidence. Reviewed labels still do not establish an exact serving checkpoint. Rejected observations are excluded from coverage previews; pending reviews, changed sources and unavailable sources remain visible. Retained coding/SOC results are shown as **Scope off; reference only** when those tracks are disabled. AISI previews retain their scenario and metric rather than producing a universal AISI score.

`No linked published result` and unavailable-source messages describe coverage gaps, not zero scores. A published zero is displayed only when the source actually reports one. The JSON state retains complete observations and history; Excel and Confluence are generated views of that state. Coverage cells are generated by Python during `export` or `refresh`; editing a score directly in Excel does not update the register or recalculate these summaries.

Some original prior/special inventory entries group several model IDs in one field. This display fix preserves those entries and the existing conservative matching rules; it does not distribute one member's score across the entire group. A result without an exact link or an existing name/version suggestion remains in the detail sheet pending an explicit mapping to an appropriate individual model record.

## 6. Record the bank decision separately

Find the record ID in Excel's added `Record ID` column or exported JSON. Generate a request bound to the current state:

```bash
python run_register.py --config config.yaml bank-template --record-id RECORD_UUID --output bank_decisions.json
```

For an actual decision, set `confirm_decision` to `true`, retain the generated `expected_vendor_sha256` **and** `expected_benchmark_sha256`, and provide the decision fields in `updates`. The benchmark hash binds the reviewed, linked evidence and selected conditional scope. Do not substitute a hash from an older template.

```json
{
  "confirm_decision": true,
  "updates": {
    "Bank Classification": "Restricted",
    "Bank Frontier Determination": "Within exclusion",
    "Bank Rationale / Open Decision": "Apply the bank's documented definition and cite the relevant evidence.",
    "Decision Owner": "Authorized bank decision owner",
    "Decision Date": "2026-11-14",
    "Approval Record": "Company governance record or ticket"
  }
}
```

This example shows the fields to edit; retain the other fields in the generated item, including both evidence hashes. A template from v1 must be regenerated before confirming a decision with the benchmark extension enabled.

```bash
python run_register.py --config config.yaml bank-update --file bank_decisions.json --actor "Authorized operator"
```

`Eligible for assessment` requires `Outside exclusion` and is still not production approval. A new confirmed decision must provide its rationale, owner, date and record reference explicitly. Administrative changes to hosting, access, use, triggers or the next review date do not silently reconfirm an old decision. Changed deployment settings, vendor fields, observed catalog metadata, reviewed benchmark evidence or selected scope can therefore keep an earlier decision marked for reassessment.

Reviewer/actor strings provide attribution, not authentication. Company access controls must restrict these commands and the authoritative state. Local hashes and history help detect accidental inconsistency; they are not a tamper-proof audit service. Use controlled pipeline permissions and your existing governance approval process for enforcement. The script does not implement a complete bank policy engine.

## 7. Publish to Confluence when you are ready

The connector supports **Confluence Cloud REST v2** and **Confluence Data Center's content REST API**. No page is created automatically. Use a dedicated existing page whose permissions are appropriate for the register.

In the Confluence editor, add these two **ordinary top-level paragraphs**, with an empty line between them:

```text
FRONTIER_REGISTER_MANAGED_START

FRONTIER_REGISTER_MANAGED_END
```

Do not put them in a code block, table, layout or macro. The publisher replaces only the content between these paragraphs. It refuses missing, duplicate, inverted or nested markers instead of replacing the whole page.

Configure the destination:

```yaml
confluence:
  enabled: true
  mode: cloud
  base_url: https://YOUR-SITE.atlassian.net/wiki
  page_id: "123456789"
  auth_kind: auto
  token_env: CONFLUENCE_API_TOKEN
  username_env: CONFLUENCE_EMAIL
  include_prior_summary: true
```

For Cloud, `auto` uses the configured email and API token. Scoped-token deployments may require the Atlassian gateway base path for your cloud ID. For Data Center, set `mode: data_center`, use the site's HTTPS base URL including its context path, and supply the personal-access token via the configured token environment variable; `auto` uses bearer authentication. An approved OAuth access token can be supplied with `auth_kind: bearer`; token acquisition/refresh is outside this package.

Inject secrets from your company's secret manager. The package does not read `.env` files, embed tokens in configuration, print credentials or follow credentialed redirects. `HTTPS_PROXY` and `SSL_CERT_FILE` can support the corporate network; TLS verification remains enabled.

Preview the proposed update:

```bash
python run_register.py --config config.yaml publish
```

This performs an authenticated read and writes `out/confluence_proposed_page.html`; it does not update Confluence. To publish:

```bash
python run_register.py --config config.yaml publish --apply
```

The publisher fetches the current version, preserves the title and published content outside the markers, and sends version + 1. It does not retry a version conflict automatically. Identical managed content produces no PUT. Publication history is kept separately from the register so publishing does not change its content revision or invalidate a review queue.

Cloud can reconcile updates with unpublished drafts. Avoid concurrent manual drafts on this dedicated page. The implementation preserves the **fetched published body** outside the markers; it cannot guarantee preservation of a separate unpublished draft. Data Center behavior must be checked against your installed version and authentication policy.

The page uses compact provider summary tables followed by current-model details, a prior/special summary and benchmark evidence sections. The benchmark sections retain score, configuration, review/mapping status and source, with AISI evidence separately presented. Excel remains the detailed filterable working view; the main register now has 44 operational columns rather than a single wide Confluence table.

## 8. Schedule it within the company

The package does not create a scheduled task on your computer or in ChatGPT. `scripts/refresh.ps1` is a Windows Task Scheduler entry point. `azure-pipelines.example.yml` illustrates a scheduled job on an approved self-hosted Azure DevOps agent.

Recommended initial operation:

1. Schedule `refresh` daily or weekly, using an account with only the source-read access it needs.
2. Route exit code 2, pending vendor/benchmark review counts and unmapped-result counts to the team's existing monitoring process.
3. Review changed findings and model mappings, and apply the separate vendor and benchmark review files through the bank's controlled process.
4. Export automatically. Enable Confluence writes only after testing the destination and service identity.

You can publish retained findings with clearly displayed monitoring warnings before every review is complete, if that is your chosen process. Publication does not approve the models. The example scheduler does not publish by default.

Keep `work/`, its template, vendor evidence and benchmark evidence snapshots together when backing up or restoring. Use a single writer or orchestration-level concurrency control. Restore a consistent snapshot and investigate a lock's recorded PID/host before manually removing a stale lock. Do not auto-delete locks after a timeout while a source fetch could still be running.

## Tests and deployment boundary

Run the offline tests without any credentials:

```bash
python run_tests.py
```

The test runner adds `src/` to its import path. Tests cover mocked HTTP behavior, pagination, evidence integrity, stale reviews, bank-field protection, decision provenance, model metadata changes, benchmark parsing and review, explicit model mapping, scope changes, safe rendering and Confluence update boundaries. The actual supplied workbook and bundled benchmark evidence have also been imported and exported locally.

For the v1.1 release, live public benchmark snapshots were downloaded using `curl`, then parsed and validated with the package. The development environment's configured SOCKS proxy prevented HTTPX requests because its optional SOCKS dependency was absent. That means live-source retrieval was checked, but it was not a successful end-to-end HTTPX network test in that environment. If your approved company proxy uses SOCKS, install the corresponding HTTPX extra through your approved repository:

```bash
python -m pip install "httpx[socks]==0.28.1"
```

An HTTP(S) proxy does not require that extra. Test your actual proxy, certificate trust, public-source access and failure reporting in the company environment before scheduling. Keep TLS verification enabled. [HTTPX proxy documentation](https://www.python-httpx.org/advanced/proxies/).

The package has not been authenticated to your provider accounts or Confluence instance. The first live environment check belongs in a company test page and approved network environment. Its dependency file records directly tested versions, not a complete transitive lockfile or a security attestation. Resolve and scan dependencies through your normal software delivery process before deployment.

The Excel exporter uses a controlled template. `openpyxl` does not preserve every possible Excel feature in arbitrary workbooks; keep custom shapes/macros outside this generated template or extend and test the exporter before introducing them.

## Project layout

```text
run_register.py                 Direct Python entry point
run_tests.py                    Offline test entry point
config.example.yaml             Paths, source hosts, watches and publisher settings
requirements-tested.txt         Tested direct dependency versions
src/frontier_register/core.py   State, review, discovery merge and bank decisions
src/frontier_register/collectors.py  Public source and vendor catalog readers
src/frontier_register/benchmarks.py  Observation state, mapping, review and decision binding
src/frontier_register/benchmark_collectors.py  Published core benchmark collectors
src/frontier_register/benchmark_views.py  Benchmark Excel and Confluence tables
src/frontier_register/render.py Excel and Confluence views
src/frontier_register/confluence.py  Managed-section publisher
src/frontier_register/cli.py    Command-line workflow
tests/                          Offline tests
data/                           Original workbook, benchmark seeds and core source snapshots
examples/                       First-run preview and validation notes
CHANGELOG.md                    Release changes and migration pointer
scripts/refresh.ps1             Windows scheduler entry point
azure-pipelines.example.yml     Self-hosted scheduled job example
```

## Official technical references

References checked on 14 November 2026. These support the integrations; they do not add new model classifications to the seed.

- OpenAI model list: https://developers.openai.com/api/reference/resources/models/methods/list
- Anthropic model list: https://platform.claude.com/docs/en/api/models/list
- Anthropic API versioning: https://platform.claude.com/docs/en/api/versioning?version=2026-10-21
- Google model list: https://aistudio.google.com/api-reference/models/list-models
- Google API authentication: https://aistudio.google.com/api-reference
- Confluence Cloud page API: https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/
- Confluence Cloud authentication: https://developer.atlassian.com/cloud/confluence/basic-auth-for-rest-apis/
- Confluence Data Center content API: https://developer.atlassian.com/server/confluence/rest/v1110/api-group-content-resource/
- Data Center personal access tokens: https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html
- openpyxl template/read-save limitations: https://openpyxl.readthedocs.io/en/stable/tutorial.html
- Azure Pipelines schedules: https://learn.microsoft.com/en-us/azure/devops/pipelines/process/scheduled-triggers
