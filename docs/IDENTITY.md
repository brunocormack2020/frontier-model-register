# Model Identity Reconciliation — v1.4: multi-source matching

This component links **observations**, not permissions or approvals. It can propose reviewer-visible relationships between Mistral/xAI API catalogue entries, curated Hugging Face repositories, and existing organization register records when an identified reviewer has sufficient evidence. It does **not** verify the bytes of a hosted checkpoint, copy benchmark scores across identities, update model eligibility or auto-approve a model. From v1.5, the separate [Foundry collector](FOUNDRY.md) can provide observed Azure deployments as additional identity nodes; reconciliation remains reviewer-only. There is **no transitive identity inference**.

## Why this matters

A developer's public API ID, an API alias, a model family, a Hugging Face repository and an immutable repository revision are **not interchangeable identifiers**. A Hugging Face repository revision is not automatically the checkpoint used by an API deployment. xAI/Mistral catalogue visibility may be account-specific; a Mistral API listing need not establish that Mistral developed the model. We therefore distinguish names, versions, hosted offerings and exact checkpoints.

The v1.2 intelligence module is still the source of observations. This module reads it and the original model register, **without modifying either**. A confirmed link is stored separately at `work/identity.json`, and the evidence hashes are bound to both endpoints. If an observation changes, a prior confirmation is displayed as `stale_evidence` until explicitly re-reviewed. Revoking a link preserves its prior review history.

## Supported relationships

| Relationship | Meaning | Does it prove identical weights? |
|---|---|---|
| `same_family` | Reviewer-supported relationship to one broader model family. | **No** |
| `same_model_version` | Reviewer-supported correspondence of a published model release/version; exact served checkpoint is not necessarily established. | **No** |
| `hosted_offering_of` | Reviewer-supported relationship between an API/deployed offering and a model/repository. Actual checkpoint may be undisclosed or change. | **No** |
| `exact_checkpoint` | Reviewer asserts exact checkpoint correspondence with an explicit authoritative basis. Requires a `proof_kind`, substantive `proof_reference`, and HTTPS `evidence_url`. | **Claim supported by reviewer evidence**, not automatically verified by software. |
| `not_same` | Reviewer rejects a proposed correspondence. | N/A |

A confirmed identity relationship is **not** a model approval. Nothing automatically transfers performance benchmarks or a frontier determination from one endpoint to another; any such use needs its own validated mapping and policy decision.

## Start: add observations and review their metadata

From the root of an existing initialized checkout (Python virtual environment already installed):

```powershell
# This needs internet for public Hugging Face metadata; enable model APIs separately if authorized.
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml intelligence-refresh

# Review candidates first; a confirmation requires each involved intelligence candidate to be acknowledged.
Copy-Item .\out\intelligence_review_queue.json .\work\intelligence_review_working.json
# Edit desired items to action="acknowledge" and add substantive review_note.
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml intelligence-review --file .\work\intelligence_review_working.json --reviewer "Metadata Reviewer"
```

The intelligence review acknowledges **source metadata**, not equivalence or approval.

## Get suggested pairs

```powershell
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml identity-status
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml identity-suggest
Copy-Item .\out\identity_review_queue.json .\work\identity_review_working.json
```

### New multi-source rules (v1.4)

Suggestions are **deterministic, source-aware and always unconfirmed**. A match is generated only within a curated developer context and only between **different source types**:

| Source pairing | Suggestion rule | Default proposed relationship | Important limitation |
|---|---|---|---|
| Mistral or xAI developer API ↔ corresponding curated Hugging Face publisher | Entire reported model ID matches repository name after case/punctuation normalization | `same_model_version` | Does **not** identify the checkpoint served by the API. |
| Developer API ↔ same-provider organization-register record | Exact reported model ID equals the registered `Exact Model ID` | `same_model_version` | Only compares the identifiers; does not establish enabled runtime version. |
| Officially curated Hugging Face publisher ↔ corresponding organization-register provider | Repository name equals registered `Exact Model ID` | `same_model_version` | Requires a registered model record for the publisher's developer. |
| Any supported pairing involving a registered model | Registered `Model Family` + `Version` exactly matches the other observed identifier after normalization | `same_family` | Weaker: a different model variant may share the same family and version fields. |
| Developer API alias ↔ corresponding publisher repository or registered model | Reported alias matches the other identifier | `hosted_offering_of` | The alias may be mutable and **must not** be treated as a frozen release. |

Default curated Hugging Face namespaces: `mistralai` (Mistral), `xai-org` (xAI), `openai` (OpenAI), `google` (Google), `meta-llama` (Meta), `deepseek-ai` (DeepSeek), `Qwen` (Qwen). These are **publisher-context filters**, not independently verified developer attestations or proof that an API serves the linked weights. Cross-developer coincidences, uncurated publishers, missing version identifiers, and arbitrary fuzzy/substring matches are blocked. There is **no transitive inference**.

The xAI publisher namespace is publicly documented at [Hugging Face xai-org](https://huggingface.co/xai-org); the OpenAI open-weight publisher is [Hugging Face openai](https://huggingface.co/openai). Actual API availability and which models are offered remain account-specific.

**A blank suggestions sheet may be correct.** Hugging Face-only discovery generally does not provide two supported *different-source* identities to reconcile. v1.4 shows an explanatory `No automatic suggestions` row instead of an empty sheet, and `identity-status` reports `source_nodes`, `suggestions_by_rule`, and `no_suggestions_reason`. To generate an API↔Hugging Face match, enable the appropriate developer API with an authorized credential and ensure the curated repository name corresponds to the observed API ID. For a registered model↔Hugging Face match, the existing register must contain that model under a corresponding provider.

The *match strength* is a transparent description of identifier evidence (`full_name`, `family_version`, or `alias`), **not** a probability, model risk score, verification, or automatic confirmation. Review each proposed relation against authoritative evidence; a suggestion has no effect on model eligibility or benchmark mappings.

Edit only the relevant queue entries. Set `action` to `confirm` or `reject`; for confirmation select a supported `relation`, add an HTTPS `evidence_url`, and explain the reviewer’s basis in `review_note`. For `exact_checkpoint`, additionally set `proof_kind` and `proof_reference` to a specific authoritative assertion (e.g. a vendor statement naming both model IDs and the artifact revision). The software validates the presence/structure of this proof **but does not establish its truth**.

```powershell
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml identity-review --file .\work\identity_review_working.json --reviewer "Identity Reviewer"
```

An old queue is rejected if any relevant source inventory or link decision has changed. Generate a fresh queue and reconcile your working changes. The `identity-review` command changes only `work/identity.json` and regenerated outputs.

## Manually propose an identity link

Automatic suggestions intentionally exclude ambiguous and unsupported naming cases. To review two other endpoints, generate an evidence-bound **manual** template:

```powershell
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml identity-template `
  --left 'intel:api:Mistral:mistral-small-3.1-24b-instruct-2503' `
  --right 'intel:hf:mistralai/Mistral-Small-3.1-24B-Instruct-2503' `
  --output .\work\manual_identity.json

# Edit action/relation/evidence_url/review_note (plus proof fields for exact_checkpoint), then:
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml identity-review `
  --file .\work\manual_identity.json --reviewer "Identity Reviewer"
```

The identifiers above are **examples**; substitute actual IDs from `out/intelligence_candidates.json`. Prefix intelligence candidate IDs with `intel:`. Existing organization inventory records may be addressed as `register:<record_id>` using the `Record ID` column of the generated Model Register sheet or the exported `out/register.json`.

To withdraw a previous link, generate a fresh template for the pair, set `action` to `revoke` and explain the reason. A confirmed relationship may not silently change to a different type: revoke and then explicitly confirm the new relationship. Previously confirmed links retain a bounded history.

## Outputs and ongoing use

Run `identity-export` or ordinary `export` **offline** to regenerate the views:

```powershell
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml identity-export
Start-Process .\out\model_register.xlsx
```

| Output | Purpose |
|---|---|
| `work/identity.json` | Authoritative, private reviewed identity relations, audit and history; **back up with `work/`**. |
| `out/identity_suggestions.json` | Named suggestions, basis and effective review status. |
| `out/identity_review_queue.json` | Evidence-bound queue for reviewer action. |
| `out/identity_links.json` | Confirmed/rejected/revoked relations with effective stale status. |
| `out/identity_summary.json` | Counts of confirmed/stale/rejected links, source-node counts, rule breakdown and no-match diagnostic. |
| **Identity Suggestions** (Excel) | Suggested relationships, match rule/strength, developer context, both node IDs, metadata review status and evidence hashes. A no-match explanation is shown when appropriate. |
| **Model Identity Links** (Excel) | Reviewer-attributed relations, proof reference, and current effective status. |

The existing 38-column register, 305 benchmark observations, AISI evidence, vendor review queues, source data and organization decisions are unchanged. **No `init` or evidence re-import is required.** A normal `intelligence-refresh` can make a confirmed identity stale if the underlying source metadata changes. Re-acknowledge the new intelligence candidate and re-review the relationship before it may be treated as current again.

## Important governance limitations

- **A model alias can float.** An API alias and a pinned Hugging Face SHA are different kinds of identifiers. Name matching never establishes byte-identical checkpoints.
- **A provider API inventory is not a model developer attestation.** The platform may offer a third-party model. Declared Hugging Face publishers are also not automatic evidence of provenance or licensing compliance.
- **`same_family` and `same_model_version` are not benchmark mappings.** A strong cybersecurity score for one snapshot must not silently be assigned to another.
- **`exact_checkpoint` is an attributed human claim**, with evidence location, proof type, hash bindings and history; there is no cryptographic download or independent verification in this release.
- **Existing application deployments are not yet inventoried** by this module. A manually recorded offering relationship does not prove which runtime version Azure Foundry, Bedrock, Vertex or a SaaS supplier currently serves.
- **Relationships are pairwise only.** A↔B and B↔C do not result in an inferred A↔C. Lack of a suggestion is **not evidence of different models**.
- **Freshness is derived from evidence identity.** The same candidate data re-fetched at a new timestamp will not reopen a confirmed link; a changed evidence SHA will.
- **Do not publish `work/identity.json`** or reviewer notes to public GitHub. It is ignored as part of `work/`.

## Troubleshooting

| Error / outcome | Suggested action |
|---|---|
| No suggestions | Read `identity-status`'s `source_nodes` and `no_suggestions_reason`. Hugging Face-only inputs generally will not match. Enable an authorized corresponding Mistral/xAI API or supply a matching registered model; manual templates cover supported cases that cannot be matched exactly. |
| Confirmation rejected because candidate evidence is pending/rejected | Acknowledge the source metadata through `intelligence-review`, then regenerate the identity queue. |
| Stale review queue | Another refresh/review changed source hashes or saved identity decisions. Regenerate queue/template rather than edit hashes. |
| `stale_evidence` | One endpoint changed its evidence digest or disappeared. Inspect the updated source and reconfirm or revoke with updated evidence. |
| `stale_candidate_review` | Original candidate's metadata review is no longer current or was rejected. Re-review the source first. |
| No model approvals changed | This is intentional. Identity reconciliation never changes bank eligibility, model benchmarking or deployment permission. |
| Workbook doesn't show new sheets | Close the workbook in Excel, update to v1.4.0+, run `identity-export`, then open **out/model_register.xlsx**. |


## Azure Foundry deployed-model observations (v1.5)

Following an authorized `foundry-refresh`, the identity engine can suggest `hosted_offering_of` relationships between actual Foundry account/project deployment observations and same-publisher registered models, developer APIs or curated Hugging Face repositories. Suggestions require full name correspondence and exclude conflicting declared versions. These matches do not establish checkpoint equivalence, contract terms, access, safety policy, or application authorization.

`identity-status`, `identity-suggest`, the review queue and the **Identity Suggestions** worksheet display these deployment nodes. Link confirmations remain evidence-bound, reviewer-attributed, and are never model onboarding approvals. Links become stale when deployment source metadata changes or the deployment is not observed in a subsequent complete scan. Absence from a scan is not proof of deletion or policy violation.

See [Azure Foundry Deployment Intelligence](FOUNDRY.md) for supported APIs, access scope, identity, Excel outputs and operational limitations.
