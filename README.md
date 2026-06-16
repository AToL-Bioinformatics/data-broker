# ATOL ENA Submission Broker

A Python CLI tool that fetches submission-ready genomic metadata from the Canopy API and submits it to the [European Nucleotide Archive (ENA)](https://www.ebi.ac.uk/ena) on behalf of ATOL organisms.

---

## Table of contents

- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
- [Commands](#commands)
  - [submit ready — all entities for an organism](#submit-ready)
  - [submit entity — single entity by type and ID](#submit-entity)
  - [submit batch — specific entities by ID](#submit-batch)
  - [resume — continue an interrupted attempt](#resume)
  - [tolid request — create first-time ToLID requests](#tolid-request)
  - [tolid poll — retry pending ToLIDs](#tolid-poll)
- [CLI execution flow](#cli-execution-flow)
- [ENA submission flow](#ena-submission-flow)
- [Prerequisite accessions](#prerequisite-accessions)
- [Hold dates](#hold-dates)
- [Dry run and validate-only](#dry-run-and-validate-only)
- [State and receipts](#state-and-receipts)
- [Architecture](#architecture)
- [Development](#development)

---

## Features

- **Bulk submission** by taxonomy ID — all ready entities in dependency order (`project → sample → experiment → run`)
- **Partial scope** — `--only samples` to submit one entity type at a time
- **Targeted submission** — one entity by type and Canopy UUID
- **Batch submission** — a specific set of entity IDs across types, inline or from a JSON file
- **Prerequisite validation** — fails immediately with a clear error if required accessions are missing; never silently auto-submits dependencies
- **Crash-safe state** — atomic writes after every checkpoint; safe to interrupt and resume at any point
- **Resume** — re-submits non-terminal entities from persisted state without re-calling Canopy
- **Canopy reporting** — every entity outcome is reported back to Canopy (`/reports/{attempt_id}`) after submission
- **ToLID workflows** — request or poll Tree of Life IDs from Canopy-backed ToLID rows
- **Hold dates** — ENA embargo date on projects and samples via `--hold-until`
- **Dry run** mode for safe pre-flight checks

---

## Installation

Requires **Python 3.11+**.

```bash
git clone <repo-url>
cd broker-test
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Verify
broker --help
```

---

## Configuration

Copy the example file and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `WEBIN_USERNAME` | ✅ | — | ENA Webin account, e.g. `Webin-12345` |
| `WEBIN_PASSWORD` | ✅ | — | ENA Webin password |
| `CANOPY_USERNAME` | ✅ | — | Canopy API username |
| `CANOPY_PASSWORD` | ✅ | — | Canopy API password |
| `CANOPY_BASE_URL` | ✅ | — | Canopy API base URL, e.g. `http://localhost:8000/api/v1` |
| `ENA_BASE_URL` | | dev server | ENA drop-box endpoint (see below) |
| `BROKER_STATE_DIR` | | `~/.broker/state` | Attempt state file directory |
| `BROKER_RECEIPT_DIR` | | `~/.broker/receipts` | Raw ENA receipt directory |
| `HTTP_TIMEOUT_SECONDS` | | `30.0` | Per-request HTTP timeout |
| `HTTP_MAX_RETRIES` | | `3` | Retries on transient transport errors |
| `HTTP_RETRY_MIN_WAIT` | | `1.0` | Min seconds between retries |
| `HTTP_RETRY_MAX_WAIT` | | `30.0` | Max seconds between retries |
| `TOLID_API_KEY` | | — | API key for the Sanger ToLID service |
| `TOLID_BASE_URL` | | staging server | ToLID API base URL |

**ENA environments:**

```bash
# Dev server — safe for testing (submissions do not go live)
ENA_BASE_URL=https://wwwdev.ebi.ac.uk/ena/submit/drop-box/submit/

# Production
ENA_BASE_URL=https://www.ebi.ac.uk/ena/submit/drop-box/submit/
```

The default is the **dev server**. Switch to production intentionally.

**ToLID environments:**

```bash
# Staging server — use this for testing
TOLID_BASE_URL=https://id-staging.tol.sanger.ac.uk

# Production
# TOLID_BASE_URL=https://id.tol.sanger.ac.uk
```

---

## Commands

### `submit ready`

Claim and submit every entity Canopy has marked ready for a given taxonomy ID, in dependency order.

```bash
broker submit ready --tax-id 9606
```

**Filter to one entity type** (`--only`):

```bash
broker submit ready --tax-id 9606 --only samples

# When prerequisites are not in the Canopy payload, supply them via flags:
broker submit ready --tax-id 9606 --only experiments \
  --project-accession PRJEB12345 \
  --sample-accession ERS123456
```

Valid `--only` values: `projects`, `samples`, `experiments`, `runs`.

> In `--only` mode, state fallback is disabled — accessions from other entities in the same attempt are not automatically propagated. Supply all prerequisites explicitly or ensure they are in the Canopy payload.

**All flags:**

| Flag | Description |
|---|---|
| `--tax-id` | Taxonomy ID (required) |
| `--only` | Restrict to one entity type |
| `--hold-until YYYY-MM-DD` | ENA embargo date for projects and samples |
| `--project-accession` | Override or supply project accession |
| `--sample-accession` | Override or supply sample accession |
| `--experiment-accession` | Override or supply experiment accession |
| `--dry-run` | Build XML but do not call ENA |
| `--validate-only` | Reserved for a validation-only flow; see note below about current implementation status |

---

### `submit entity`

Claim and submit one specific entity by type and Canopy UUID.

```bash
# Project — no prerequisites needed
broker submit entity --type project --id <uuid>

# Sample
broker submit entity --type sample --id <uuid> \
  --project-accession PRJEB12345

# Experiment
broker submit entity --type experiment --id <uuid> \
  --project-accession PRJEB12345 \
  --sample-accession ERS123456

# Run
broker submit entity --type run --id <uuid> \
  --experiment-accession ERX222222
```

Valid `--type` values: `project`, `sample`, `experiment`, `run`.

**All flags:**

| Flag | Description |
|---|---|
| `--type` | Entity type (required) |
| `--id` | Canopy entity UUID (required) |
| `--project-accession` | Override or supply project accession |
| `--sample-accession` | Override or supply sample accession |
| `--experiment-accession` | Override or supply experiment accession |
| `--hold-until YYYY-MM-DD` | ENA embargo date (projects and samples only) |
| `--dry-run` | Build XML but do not call ENA |

---

### `submit batch`

Claim and submit a hand-picked set of entities by ID. Accepts IDs inline or from a JSON file.

#### Inline — comma-separated IDs per type

```bash
broker submit batch --samples abc-123,def-456 --experiments xyz-789

# Multiple types
broker submit batch \
  --projects p-uuid \
  --samples  s-uuid-1,s-uuid-2 \
  --runs     r-uuid-1,r-uuid-2,r-uuid-3
```

#### From a JSON file

```bash
broker submit batch --from-file batch.json
```

**`batch.json` format:**

```json
{
  "projects":    ["uuid1"],
  "samples":     ["uuid2", "uuid3"],
  "experiments": ["uuid4"],
  "runs":        ["uuid5", "uuid6"]
}
```

Any key can be omitted if you have no entities of that type.

#### Combined — file as base, inline adds more

```bash
broker submit batch --from-file batch.json --runs extra-run-id
```

Duplicate IDs across file and inline flags are de-duplicated automatically.

**All flags:**

| Flag | Description |
|---|---|
| `--projects` | Comma-separated project IDs |
| `--samples` | Comma-separated sample IDs |
| `--experiments` | Comma-separated experiment IDs |
| `--runs` | Comma-separated run IDs |
| `--from-file` | Path to a JSON file with entity IDs |
| `--project-accession` | Override or supply project accession |
| `--sample-accession` | Override or supply sample accession |
| `--experiment-accession` | Override or supply experiment accession |
| `--hold-until YYYY-MM-DD` | ENA embargo date (projects and samples only) |
| `--dry-run` | Build XML but do not call ENA |

> Like `submit entity`, batch mode disables state fallback — all prerequisites must be explicit.

---

### `resume`

Resume a previously interrupted attempt. Loads persisted state, skips entities that already succeeded, and re-submits the rest. Canopy is **not** re-called — payloads are read from the local state file.

```bash
broker resume --attempt-id <uuid>

# If an accession is now available that was missing before:
broker resume --attempt-id <uuid> --project-accession PRJEB12345
```

ENA Webin is idempotent on submission alias — re-posting an entity that was already accepted safely returns the same accession. This is the property that makes crash-safe resume correct.

---

### `tolid request`

Request Tree of Life IDs for Canopy rows in `not_requested` state.

```bash
broker tolid request

# Restrict to one taxon
broker tolid request --tax-id 1931064

# Restrict to one Canopy sample row
broker tolid request --sample-id <uuid>

# Fetch ToLIDs but do not send MODIFY submissions back to ENA
broker tolid request --tax-id 1931064 --no-update-ena
```

This command asks Canopy for ToLID work items, calls the Sanger ToLID API for each row, then reports the result back to Canopy:
- `assigned` with a real ToLID
- `pending` with a request ID and updated `last_requested_at`

If `--update-ena` is enabled and a ToLID is assigned, the broker also attempts an ENA `MODIFY` submission to add the `tolid` sample attribute. The ToLID is still reported back to Canopy even if ENA `MODIFY` fails.

**All flags:**

| Flag | Description |
|---|---|
| `--tax-id` | Optional taxon filter |
| `--sample-id` | Optional Canopy sample ID filter |
| `--limit` | Optional maximum number of rows to process |
| `--update-ena / --no-update-ena` | Whether to send an ENA `MODIFY` after each assigned ToLID |

### `tolid poll`

Retry ToLIDs for Canopy rows already in `pending` state.

```bash
broker tolid poll

# Restrict to one taxon
broker tolid poll --tax-id 1931064

# Restrict to one Canopy sample row
broker tolid poll --sample-id <uuid>
```

This command does not decide which rows are due for retry. It simply polls whatever pending rows Canopy returns, re-posting the same Sanger `request/create` call for each one.

**All flags:**

| Flag | Description |
|---|---|
| `--tax-id` | Optional taxon filter |
| `--sample-id` | Optional Canopy sample ID filter |
| `--limit` | Optional maximum number of rows to process |
| `--update-ena / --no-update-ena` | Whether to send an ENA `MODIFY` after each assigned ToLID |

---

## CLI Execution Flow

The sections below describe what each CLI command actually does internally, in order.

### `broker submit ready`

1. Parse CLI flags and validate `--hold-until`, `--only`, and submission mode.
2. Build clients and services from `.env` / environment settings.
3. Call `CanopyClient.claim_by_tax_id()` to claim all ready entities for the taxonomy ID.
4. Translate the Canopy claim response into an `AttemptState`, grouped by entity type.
5. Persist the initial attempt JSON to the state store.
6. Iterate entities in dependency order: `project -> sample -> experiment -> run`.
7. For each entity:
   - resolve prerequisite accessions from CLI overrides, Canopy payload, and optionally prior succeeded entities in the same attempt
   - mark the entity `SUBMITTED` and save state
   - in `--dry-run`, mark it `SKIPPED` and stop there
   - otherwise build ENA XML, submit to ENA, save the raw receipt, then mark `SUCCEEDED` or `FAILED`
8. After iteration finishes or aborts, batch-report entity outcomes back to Canopy and finalise the claim lease.
9. Recompute overall attempt status, save the final state, render the Rich table, and exit non-zero on `failed` or `partial`.

`--only` changes step 3 and step 7:
- only the requested entity type is claimed from Canopy
- state fallback for prerequisites is disabled, so dependencies must already be in the payload or provided via flags

### `broker submit entity`

1. Parse `--type`, `--id`, and any accession override flags.
2. Build clients and services from settings.
3. Call `CanopyClient.claim_entity()` for the specific entity.
4. Build and save a targeted-mode `AttemptState`.
5. Submit the claimed entity through the same per-entity lifecycle as `submit ready`.
6. Report the outcome to Canopy, finalise the claim, save final state, render the result table, and exit non-zero on failure.

Key difference from `submit ready`: state fallback is always disabled. Missing prerequisites must come from the claimed payload or explicit CLI flags.

### `broker submit batch`

1. Parse inline `--projects`, `--samples`, `--experiments`, `--runs`, plus optional `--from-file`.
2. Merge file-based IDs with inline IDs and de-duplicate them.
3. Reject the command if no IDs remain after parsing.
4. Build clients and services from settings.
5. Call `CanopyClient.claim_batch()` with the selected IDs.
6. Build and save a targeted-mode `AttemptState` containing all claimed entities.
7. Submit each claimed entity through the same per-entity lifecycle used by the other submission commands.
8. Report outcomes to Canopy, finalise the claim, save final state, render the result table, and exit non-zero on failure.

Like `submit entity`, batch mode disables state fallback. Prerequisites must already exist in the payload or be supplied via CLI flags.

### `broker resume`

1. Parse `--attempt-id` and any new accession override flags.
2. Load broker settings and open the local state store.
3. Load the saved `AttemptState` JSON from disk. Canopy is not queried again.
4. If the attempt is already `COMPLETED`, render the saved result and exit.
5. Determine whether state fallback is allowed from the original attempt mode:
   - bulk attempts allow state fallback
   - targeted and batch attempts do not
6. Revisit entities in canonical dependency order and skip terminal ones (`SUCCEEDED`, `SKIPPED`).
7. Re-submit every non-terminal entity through the same submission lifecycle used in a normal run.
8. Report all reportable entity outcomes to Canopy, including entities that were already terminal before resume started.
9. Finalise the claim, recompute attempt status, save state, render results, and exit non-zero on failure.

### `broker tolid request`

1. Parse optional `--tax-id`, `--sample-id`, `--limit`, and whether ENA should be updated.
2. Build the Canopy, ToLID, and ENA clients and verify `TOLID_API_KEY` is configured.
3. Call `CanopyClient.list_requestable_tolids()` to fetch rows in `not_requested` state.
4. For each returned row:
   - call the Sanger ToLID `POST /api/v3/request/create` endpoint using `specimen_id`, `taxon_id`, and optional `scientific_name`
   - if the response is `pending`, report `pending` plus `request_id` and `last_requested_at` back to Canopy
   - if the response is `assigned`, optionally send ENA `MODIFY`, then report `assigned` plus the new `tolid` back to Canopy
5. Render a ToLID results table and exit non-zero if any request returned an error.

### `broker tolid poll`

1. Parse optional `--tax-id`, `--sample-id`, `--limit`, and whether ENA should be updated.
2. Build the Canopy, ToLID, and ENA clients and verify `TOLID_API_KEY` is configured.
3. Call `CanopyClient.list_pending_tolids()` to fetch rows in `pending` state.
4. For each returned row:
   - re-post the same Sanger ToLID `POST /api/v3/request/create` call
   - if the response remains `pending`, report the refreshed `request_id` and `last_requested_at` back to Canopy
   - if the response is `assigned`, optionally send ENA `MODIFY`, then report `assigned` plus the new `tolid` back to Canopy
5. Render a ToLID results table and exit non-zero if any request returned an error.

Unlike the submission commands, the ToLID commands do not use broker attempt state as their source of truth. Canopy owns the durable ToLID rows, and the broker acts only as the ToLID worker.

---

## ENA submission flow

This is the end-to-end flow for normal submission of metadata into ENA.

1. Broker claims submission-ready entities from Canopy.
2. Broker builds a local `AttemptState` from the claim response and saves it.
3. Broker processes entities in dependency order: `project -> sample -> experiment -> run`.
4. Before each submission, the broker resolves prerequisite accessions from:
   - CLI override flags
   - Canopy prerequisites in the claim payload
   - previously succeeded entities in the same bulk attempt, when state fallback is allowed
5. Broker transforms the Canopy payload into ENA XML using `TransformService`.
6. Broker builds the ENA `SUBMISSION` XML wrapper, including `HOLD` for projects and samples when `--hold-until` is set.
7. Broker submits the XML to the ENA Webin drop-box.
8. Broker stores the raw ENA receipt and updates the entity state to `SUCCEEDED` or `FAILED`.
9. After the run completes, broker reports all outcomes back to Canopy and finalises the Canopy claim.
10. Optional ToLID work happens later via `broker tolid request` or `broker tolid poll`, not during the per-entity ENA submission lifecycle.

---

## Prerequisite accessions

ENA submissions follow a strict dependency chain:

```
project → sample → experiment → run
```

| Entity | Required accessions |
|---|---|
| `project` | none |
| `sample` | `project_accession` (PRJEB\*) |
| `experiment` | `project_accession` (PRJEB\*) + `sample_accession` (ERS\*) |
| `run` | `experiment_accession` (ERX\*) |

Resolution priority (highest wins):

1. **CLI flags** (`--project-accession`, `--sample-accession`, `--experiment-accession`)
2. **Canopy payload** (`prerequisites` field on the entity from the claim response)
3. **State fallback** — accession from a succeeded entity in the same attempt *(bulk full-scope only)*
4. **Fail** with a clear `PrerequisiteMissingError` naming the exact missing fields

State fallback (3) is disabled in `submit entity`, `submit batch`, and `submit ready --only`. Supply missing accessions via flags or ensure Canopy has them in the payload.

---

## Hold dates

Set an ENA embargo date on projects and samples. The entity stays private until the specified date.

```bash
broker submit ready  --tax-id 9606   --hold-until 2027-01-01
broker submit entity --type project  --id <uuid> --hold-until 2027-06-30
broker submit batch  --projects <id> --hold-until 2027-06-30
```

Format: `YYYY-MM-DD`. The hold date is stored in the attempt state file so `broker resume` preserves the original date.

---

## Dry run and validate-only

```bash
# Dry run — claims from Canopy, builds XML, but does NOT call ENA or report back
broker submit ready --tax-id 9606 --dry-run

# Validate only — accepted by the CLI, but not yet fully wired as a submission bypass
broker submit ready --tax-id 9606 --validate-only
```

`--dry-run` and `--validate-only` are mutually exclusive.

Current behavior:
- `--dry-run` checkpoints entities and skips the ENA POST.
- `--validate-only` is accepted and converted into a submission mode value, but the submission path does not yet stop early on that mode. Do not rely on it as a true validation-only execution path until the implementation is completed.

---

## State and receipts

| Kind | Default path | Contents |
|---|---|---|
| **Attempt state** | `~/.broker/state/<attempt-id>.json` | Per-entity status, accessions, error messages, raw payloads. Used by `broker resume`. |
| **ENA receipts** | `~/.broker/receipts/<attempt-id>/<type>_<entity-id>.txt` | Verbatim ENA response (XML or JSON) for every submission attempt. |

ToLID workflow state is not persisted in broker attempt files. Durable ToLID status lives in Canopy and is accessed through the `broker tolid request` and `broker tolid poll` commands.

State writes are **atomic** — the broker writes to a `.tmp` file then calls `os.replace()`. A crash at any point leaves either the old state or the new state, never a corrupt file.

Inspect a saved attempt:

```bash
cat ~/.broker/state/<attempt-id>.json | python3 -m json.tool
```

Override storage paths with `BROKER_STATE_DIR` and `BROKER_RECEIPT_DIR` in `.env`.

---

## Architecture

```
src/broker/
├── cli.py                         Typer CLI commands; manual DI wiring
├── config.py                      Pydantic Settings (env vars / .env)
├── enums.py                       EntityType, AttemptMode, status enums
├── errors.py                      BrokerError exception hierarchy
│
├── clients/
│   ├── canopy.py                  Canopy API client
│   │                                auth: form-encoded login → JWT bearer
│   │                                endpoints: /claims/*, /validation, /reports/*, /tolids/*
│   ├── ena.py                     ENA Webin drop-box client
│                                    basic auth, multipart POST, never raises
│   └── tolid.py                   Sanger ToLID request/create client
│
├── models/
│   ├── attempt.py                 AttemptState + EntitySubmissionState (persisted to disk)
│   ├── canopy.py                  Canopy API models + internal transform DTO
│   └── ena.py                     ENASubmissionResult, ENAAccessions
│
├── services/
│   ├── orchestrator.py            run_bulk / run_targeted / run_batch
│   ├── submission_service.py      Single-entity 7-step lifecycle (checkpointed)
│   ├── resume_service.py          Resume from persisted state
│   ├── prerequisite_validation.py Accession resolution with priority chain
│   ├── transform_service.py       Canopy payload → ENA XML (lxml; no f-strings)
│   ├── receipt_parser.py          ENA XML/JSON receipt → ENASubmissionResult
│   ├── report_service.py          Build ReportBatchPayload and send to Canopy
│   └── tolid_service.py           Canopy-backed ToLID request / poll worker
│
└── storage/
    ├── state_store.py             Atomic JSON state persistence
    └── receipt_store.py           Verbatim receipt file storage
```

### Submission lifecycle (per entity)

Each entity goes through these steps inside `SubmissionService.submit_entity()`:

```
1. Resolve prerequisites       → raise PrerequisiteMissingError if missing (nothing saved yet)
2. Mark SUBMITTED + save       ← crash-safe checkpoint
3. Build ENA XML               via TransformService (lxml; alias = broker-{type}-{id})
4. POST to ENA                 → ENASubmissionResult (never raises)
5. Save raw receipt            to ReceiptStore
6. Mark SUCCEEDED/FAILED       + save state
```

If the process crashes between steps 2 and 6, `broker resume` will re-POST to ENA. Because ENA Webin is **idempotent on alias**, the same alias returns the same accession rather than creating a duplicate.

Outcome reporting back to Canopy happens after the run finishes, in the orchestrator / report service layer, not inside the single-entity submission lifecycle itself.

### Canopy API endpoints used

| Endpoint | Method | Purpose |
|---|---|---|
| `/auth/login` | POST | Obtain JWT access + refresh tokens (form-encoded) |
| `/auth/refresh` | POST | Refresh access token |
| `/broker/claims/ready` | POST | Claim all ready entities for a `tax_id` |
| `/broker/claims/entity` | POST | Claim a single entity by type + ID |
| `/broker/claims/batch` | POST | Claim specific entities by ID across types |
| `/broker/validation` | POST | Validate prerequisite accessions for an entity |
| `/broker/reports/{attempt_id}` | POST | Report submission outcomes (batch; `attempt_id` path-only) |
| `/broker/tolids/requestable` | GET | Fetch ToLID rows in `not_requested` state |
| `/broker/tolids/pending` | GET | Fetch ToLID rows in `pending` state |
| `/broker/tolids/{sample_id}` | GET | Fetch one ToLID row, including sample payload when available |
| `/broker/tolids/{sample_id}/report` | POST | Report `pending` or `assigned` ToLID results |

### Report status values

The broker maps internal states to the three canonical contract values:

| Internal status | Reported as |
|---|---|
| `SUCCEEDED` | `accepted` |
| `FAILED` | `rejected` |
| `SUBMITTED` (checkpoint) | `submitting` |

---

## Development

```bash
# Install with dev extras
pip install -e ".[dev]"

# Run all tests
pytest

# With coverage
pytest --cov=src/broker --cov-report=term-missing

# Lint
ruff check src/ tests/

# Type check
mypy src/
```

Tests use `pytest-httpx` to mock all HTTP calls — no real network or credentials needed.

### Adding a new entity type

1. Add the value to `EntityType` in `enums.py` and `ENTITY_DEPENDENCY_ORDER`
2. Add prerequisite rules in `prerequisite_validation.py` (`_PREREQUISITES`, `_FIELD_TO_TYPE`)
3. Add an XML builder in `transform_service.py`
4. Add a submit method in `clients/ena.py` and wire it in `submission_service._dispatch_to_ena()`
5. Update `orchestrator._build_attempt_state()` if the new type has unique prerequisite fields
