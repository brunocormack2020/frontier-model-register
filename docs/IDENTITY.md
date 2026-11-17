# Model Identity Reconciliation — v1.3

This component links **observations**, not permissions or approvals. It can reconcile a Mistral API model, a curated Hugging Face repository and a record already present in the organization register when an identified reviewer has sufficient evidence. It does **not** verify the bytes of a hosted checkpoint, discover Azure deployments, copy benchmark scores across identities, update model eligibility or auto-approve a model. There is **no transitive identity inference**.

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

Initial suggestions are **limited and deterministic**: an exact normalized Mistral API ID (or its declared alias) that matches a repository name within the curated `mistralai` Hugging Face publisher namespace. A matching name is never sufficient to establish that an API serves the same model weights or revision. Mutable alias suggestions are explicitly labeled as such. There is no speculative cross-provider fuzzy matching, no model-family-guessing engine, and no automatic links to unverified xAI repositories.

Edit only the relevant queue entries. Set `action` to `confirm` or `reject`; for confirmation select a supported `relation`, add an HTTPS `evidence_url`, and explain the reviewer’s basis in `review_note`. For `exact_checkpoint`, additionally set `proof_kind` and `proof_reference` to a specific authoritative assertion (e.g. a vendor statement naming both model IDs and the artifact revision). The software validates the presence/structure of this proof **but does not establish its truth**.

```powershell
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml identity-review --file .\work\identity_review_working.json --reviewer "Identity Reviewer"
```

An old queue is rejected if any relevant source inventory or link decision has changed. Generate a fresh queue and reconcile your working changes. The `identity-review` command changes only `work/identity.json` and regenerated outputs.

## Manually propose an identity link

Automatic suggestions intentionally cover only a narrow naming case. To review two other endpoints, generate an evidence-bound **manual** template:

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
| `out/identity_summary.json` | Counts of confirmed, stale, rejected and proposed links. |
| **Identity Suggestions** (Excel) | Potential cross-source name matches and evidence references. |
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
| No suggestions | Run `intelligence-refresh`, then `identity-suggest`; check that both matching Mistral/HF sources were discovered. Many legitimate relationships require manual templates. |
| Confirmation rejected because candidate evidence is pending/rejected | Acknowledge the source metadata through `intelligence-review`, then regenerate the identity queue. |
| Stale review queue | Another refresh/review changed source hashes or saved identity decisions. Regenerate queue/template rather than edit hashes. |
| `stale_evidence` | One endpoint changed its evidence digest or disappeared. Inspect the updated source and reconfirm or revoke with updated evidence. |
| `stale_candidate_review` | Original candidate's metadata review is no longer current or was rejected. Re-review the source first. |
| No model approvals changed | This is intentional. Identity reconciliation never changes bank eligibility, model benchmarking or deployment permission. |
| Workbook doesn't show new sheets | Close the workbook in Excel, update to v1.3.0+, run `identity-export`, then open **out/model_register.xlsx**. |
