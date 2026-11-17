# Vendor Model Intelligence — v1.2 (discovery-only)

## Scope and safety boundary

This module discovers and tracks **candidate** models and repository metadata from Mistral, xAI, and an explicitly curated Hugging Face watchlist. It **does not** approve models, change the model-register decisions, execute inference, execute downloaded code, or download weights. API access is account-scoped, so it is **not** a definitive global catalogue. Disappearance from a catalogue is **never interpreted as retirement**.

**Important distinction:** A model visible in the Mistral API may be a *third-party developer's model*. The developer API identifies the offering visible to that account, not necessarily the originating developer or model checkpoint. API metadata includes `catalogue_provider` and `developer_identity_confirmed: false` for this reason. Hugging Face repository ownership is a publishing claim, not an independently validated provenance or safety attestation. The intelligence collector itself never merges identities or makes model approvals. In v1.4, a separate [Model Identity Reconciliation](IDENTITY.md) workflow proposes **review-only** cross-source matches, with evidence-bound reviewer confirmation; model weights and hosted checkpoints are not automatically verified.

Official API references: [Mistral List Models](https://docs.mistral.ai/api/endpoint/models), [xAI List Language Models](https://docs.x.ai/developers/rest-api-reference/inference/models), [Hugging Face HfApi author/list and metadata](https://huggingface.co/docs/huggingface_hub/main/package_reference/hf_api).

## Configuration and first run

`config.example.yaml` includes an `intelligence:` block. Its defaults are:

- `enabled: true` with **Hugging Face public metadata** collection enabled.
- Mistral and xAI API discovery **disabled until you enable it and supply credentials**.
- A curated publisher watchlist: `mistralai`, `meta-llama`, `deepseek-ai`, `Qwen`, `xai-org`, and `openai`, plus explicit repository IDs.
- Publisher queries capped at 10 per author. If the source returns exactly the cap, status is **partial**, and **no completeness or retirement inference** is made. Increase the cap deliberately or narrow the watchlist; the selected repository IDs are checked independently.

Enable API discovery under `intelligence.apis.mistral.enabled` and/or `.xai.enabled`. In PowerShell, set **read-only model-list API keys** as environment variables, rather than storing them in YAML or the repository:

```powershell
$env:MISTRAL_API_KEY = '<your Mistral key>'
$env:XAI_API_KEY = '<your xAI key>'
# Optional: only for approved access to gated/private Hub metadata.
$env:HF_TOKEN = '<approved Hugging Face token>'
```

If possible, use your secret manager to inject credentials into the process. The module only accesses:

- `GET https://api.mistral.ai/v1/models`
- `GET https://api.x.ai/v1/language-models`
- `GET https://huggingface.co/api/models` (bounded author filter)
- `GET https://huggingface.co/api/models/{publisher}/{repo}` (explicitly configured repo)

No inference endpoints are called, and no arbitrary URLs from provider responses are followed. Metadata requests do not fetch model weights. Requests reject redirects, impose timeout/size limits, validate repository slugs, and avoid printing tokens or response bodies.

Run from the repository root **after the original `init` step is complete**:

```powershell
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml intelligence-refresh
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml intelligence-status
Start-Process .\out\model_register.xlsx
```

With no credentials, you can leave Mistral/xAI disabled and run Hugging Face metadata collection only. Enable each developer adapter separately when ready.

> **Never re-run `init` to enable vendor intelligence.** Intelligence state is additive and stored in `work/intelligence.json`, distinct from the existing model register and organization decisions. Existing `work/register.json` and the saved benchmark results are not altered by an intelligence-only refresh.

## Review and outputs

A successful refresh adds or updates observations and generates three **additional worksheets** in the usual `out/model_register.xlsx` export:

| Worksheet | Contents |
|---|---|
| **Vendor Intelligence** | Candidate ID, reported model/repository, source, version/hash, claimed license, gate status, dates and review status |
| **Intelligence Events** | New candidates, material metadata changes, source failures and reviewer acknowledgements |
| **Intelligence Sources** | Last attempt, last success, source completeness/health, bounded result counts and warnings |

It also creates `out/intelligence_candidates.json`, `out/intelligence_events.json`, `out/intelligence_review_queue.json` and `out/intelligence_summary.json`.

Review each new or changed candidate by copying the review queue **outside `out/`** before the next refresh and editing only the `action` and `review_note` fields:

```powershell
Copy-Item .\out\intelligence_review_queue.json .\intelligence_review_working.json
# Edit each chosen item: action = "acknowledge" or "reject", with a meaningful review_note.
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml intelligence-review --file .\intelligence_review_working.json --reviewer "Named reviewer"
```

Reviewing intelligence acknowledges the *metadata*, not the model's eligibility. It does **not** promote candidates into the approved register. A source metadata change reopens pending review. Stale or forged queues are rejected.

To regenerate intelligence worksheets and JSON **without network access**:

```powershell
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml intelligence-export
```

The normal `export` also includes intelligence views whenever `work/intelligence.json` exists.

## Interpretation limitations and expected errors

| Symptom | Meaning and action |
|---|---|
| Missing Mistral/xAI models | Provider discovery is opt-in; enable the relevant adapter and inject the right key. Account API visibility is not global model availability. |
| Source unavailable, HTTP 401/403 | Credential not authorized or resource inaccessible. Prior candidates remain; inspect the provider account and permissions. |
| Source unavailable, HTTP 429 | Provider rate limit. Do not treat previously observed models as absent or retired. |
| Hugging Face source `partial` | The configured latest-model cap may omit other matching releases; use explicit repository IDs for important models and widen the cap carefully. |
| A publisher repository is gated | Its metadata may remain visible. This module does not bypass gates or download artifacts. |
| Different metadata on repeated runs | Check the source, version SHA, declared license, and conditions; a change is an evidence review trigger, not an automatic risk-rating change. |
| Same underlying model appears on multiple sources | Remains **separate candidate identities** until an attributed reconciliation workflow validates the relationship. |
| No new events | Stable metadata does not create duplicate change events. Source checks and retrieval timestamps can still update. |

**Exit codes:** `0` for a completed run (including explicitly bounded/partial lists); `2` when any enabled source was unavailable; `1` for local state/configuration/command errors. Do not use an unavailable/partial catalogue as authoritative inventory completeness evidence.

**Deployment limitation:** This release does not discover actual model deployments in Azure Foundry, Bedrock, or Vertex, or model dependencies inside third-party SaaS. It does not scan model files, infer capability ratings, evaluate licensing compliance, or compare hosted aliases to pinned checkpoint revisions. Those remain separate workstreams.
