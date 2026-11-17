# Azure Foundry Deployment Intelligence — v1.5.0

This optional, **read-only** module inventories Microsoft Foundry / Azure AI Services deployments using two documented APIs. It does **not** create or modify deployments, inspect prompts, run inference, approve a model, apply Azure Policy, or establish a particular checkpoint's identity.

## Collection boundaries and APIs

| Scope | HTTP request | Entra token audience |
|---|---|---|
| Azure ARM subscription | `GET https://management.azure.com/subscriptions/<UUID>/providers/Microsoft.CognitiveServices/accounts?api-version=2025-06-01` then account deployments | `https://management.azure.com/.default` |
| ARM account (explicit) | `GET https://management.azure.com/subscriptions/<UUID>/resourceGroups/<RG>/providers/Microsoft.CognitiveServices/accounts/<NAME>/deployments?api-version=2025-06-01` | `https://management.azure.com/.default` |
| Foundry project endpoint | `GET https://<RESOURCE>.services.ai.azure.com/api/projects/<PROJECT>/deployments?api-version=v1` | `https://ai.azure.com/.default` |

Official references: [ARM deployment list](https://learn.microsoft.com/en-us/rest/api/microsoftfoundry/accountmanagement/deployments/list?view=rest-microsoftfoundry-accountmanagement-2025-06-01), [ARM account list](https://learn.microsoft.com/en-us/rest/api/microsoftfoundry/accountmanagement/accounts/list?view=rest-microsoftfoundry-accountmanagement-2025-06-01), [Foundry project deployments](https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/aiproject), [Foundry authentication and RBAC](https://learn.microsoft.com/en-us/azure/foundry/concepts/authentication-authorization-foundry).

ARM account deployments and project-visible deployments are *separate observations*. They may overlap but the project endpoint alone does not establish the ARM resource ID, and the same deployment name does not establish that two observations refer to the same runtime.

**Limitations:** A deployment does not establish application use, active traffic, data processing location, vendor contract terms, enabled guardrails, actual checkpoint weights, or compliance with an organization's model policy. The Azure account's `location` is labelled **Resource Region**, not inference-processing geography. ARM `model.format` is a publisher/format *proxy* when an explicit `model.publisher` is absent; it is not an independent developer attestation.

## 1. Configure an existing register (never reinitialize)

Update your local clone to v1.5.0, then update dependencies:

```powershell
cd "$env:USERPROFILE\frontier\frontier-model-register"
git pull --ff-only origin main
.\.venv\Scripts\python.exe -m pip install -r .\requirements-tested.txt
```

Merge the following **single** `foundry:` block into the existing local `config.yaml`; do not replace your existing `config.yaml` or `work/` with the example. Leave it disabled until at least one scope and a read-only identity are configured:

```yaml
foundry:
  enabled: false
  subscriptions: []
  accounts: []
  projects: []
  timeout_seconds: 20
  max_pages: 25
  max_accounts: 100
  max_deployments: 1500
  max_response_bytes: 3000000
```

Examples: use **one or more scopes** you have permission to read:

```yaml
foundry:
  enabled: true
  subscriptions:
    - 00000000-1111-2222-3333-444444444444
  accounts: []
  projects: []
  timeout_seconds: 20
  max_pages: 25
  max_accounts: 100
  max_deployments: 1500
  max_response_bytes: 3000000
```

Or, to avoid scanning a whole subscription:

```yaml
foundry:
  enabled: true
  subscriptions: []
  accounts:
    - /subscriptions/00000000-1111-2222-3333-444444444444/resourceGroups/example-rg/providers/Microsoft.CognitiveServices/accounts/examplefoundry
  projects:
    - https://examplefoundry.services.ai.azure.com/api/projects/security
  timeout_seconds: 20
  max_pages: 25
  max_accounts: 100
  max_deployments: 1500
  max_response_bytes: 3000000
```

**Do not configure both explicit ARM account discovery and whole-subscription discovery for the same subscription**. The module rejects overlapping scopes to prevent duplicate/conflicting evidence. Project endpoints may provide a *different* perspective on the same service, but are retained separately. All endpoints must follow the documented Azure host/path patterns; no arbitrary URL or cross-host pagination is accepted.

## 2. Authenticate securely with Microsoft Entra

The collector uses `DefaultAzureCredential` from `azure-identity`; no account keys, model API keys, shared secrets, or tokens are written to disk. For local work on Windows, a permitted Azure CLI login can supply credentials:

```powershell
az login --tenant <approved-tenant-ID>
az account set --subscription <approved-subscription-ID>
az account show --query "{subscription:id, tenant:tenantId}" -o json
```

Choose only subscriptions/projects where the bank has explicitly authorized inventory access. For ARM inventory, request **Reader** or a narrower approved custom role permitting account/deployment read operations at the appropriate scope. Project access requires an appropriate Foundry project role/permission for listing deployments; ARM Reader access does not guarantee project API visibility. Conditional Access, network configuration, private endpoints and tenant requirements may affect connectivity.

For unattended jobs, use an approved managed identity/workload identity with the equivalent read permissions. Avoid committing credentials, exporting long-lived bearer tokens or using a broad Contributor role. `DefaultAzureCredential` excludes interactive browser prompting in this module; you must have an available permitted credential method already configured.

## 3. Run collection and inspect outputs

```powershell
# Fetch configured ARM/project deployment lists (no inference; GET requests only)
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml foundry-refresh

# Local status: no network
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml foundry-status

# Recalculate identity suggestions from newly observed deployments: offline
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml identity-suggest

# Open generated workbook
Start-Process .\out\model_register.xlsx
```

To reproduce JSON and worksheets *without Azure network access*:

```powershell
.\.venv\Scripts\python.exe .\run_register.py --config .\config.yaml foundry-export
```

Output is retained locally (and ignored by Git): `work/foundry.json`, `out/foundry_deployments.json`, `out/foundry_events.json`, `out/foundry_sources.json`, `out/foundry_summary.json`. The Excel export adds **Foundry Deployments**, **Deployment Intelligence Events**, **Foundry Sources**, and **Foundry Screening** (review-only potential exposure indicators; never automated compliance approval). The existing **Identity Suggestions** view can also show matched Foundry-to-register/developer/repository observations as **unconfirmed hosted-offering suggestions**, subject to evidence-based review.

The existing bank/organization model state, 305 benchmark observations, AISI findings, identity approvals and model eligibility decisions remain separate. **Do not run `init`, delete `work/` or copy state files from a different checkout to enable this functionality.**

## 4. How to interpret changes

- **NEW_DEPLOYMENT:** first observation of a deployment in the configured scope.
- **DEPLOYMENT_CHANGED:** previously observed metadata (model, version, SKU, upgrade option, RAI policy, etc.) changed; old key facts remain in bounded history.
- **NOT_OBSERVED_ON_RESCAN:** previous deployment absent from a *complete* successful scan. This is **not proof of deletion** and needs verification.
- **SOURCE_PARTIAL / SOURCE_UNAVAILABLE:** collection or access incomplete; previous observations remain stored and are not silently removed.
- **DEPLOYMENT_REOBSERVED:** previously unobserved deployment reappeared in a successful or partial scan.

`foundry-refresh` returns exit code `0` if all scopes complete, `2` if any source is partial/unavailable, or `1` for invalid configuration or local state. A partial source does **not** authorize a compliance determination from an incomplete inventory. The collector does not automatically discover project endpoints from ARM or assume access to every Azure tenant, subscription, or project.

## 5. Identity review and policy boundaries

Foundry model publisher/name/version can generate a proposed `hosted_offering_of` relation to an existing registered model or curated developer observation. The system blocks a suggestion if explicit reported version fields conflict. Name agreement does not prove checkpoint identity, tenant location, hosting data processing, or safety controls.

Review these suggestions using the existing `identity-suggest`, `identity-template` and `identity-review` workflow documented in [Model Identity Reconciliation](IDENTITY.md). A confirmed link remains reviewer-attributed and is not an organization model approval. Links become stale if deployment evidence changes or a previously observed deployment is not found in a complete rescan. Confirmation of an unobserved deployment is blocked.

**Future work:** Azure Policy compliance assessment, application-to-deployment and Agent 365 relationships, tenant-wide coverage analysis, third-party SaaS dependencies and automated reassessment routing. v1.5 is explicitly **read-only discovery and reporting**, not a bank policy enforcement engine.
