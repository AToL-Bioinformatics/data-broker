# ENA Submission Broker

A Python CLI that retrieves submission-ready genomic metadata from the Canopy API and submits it to ENA (European Nucleotide Archive).

## Features

- **Bulk submission** by taxonomy ID, in dependency order (project → sample → experiment → run)
- **Partial bulk scope** — submit only one entity type at a time
- **Targeted submission** — submit a single entity by type and ID
- **Prerequisite validation** — fails immediately with a clear error if required accession fields are missing; never auto-submits dependencies
- **Crash-safe state persistence** — atomic JSON writes after every external call; supports resume after interruption
- **Raw receipt storage** — ENA receipts saved verbatim to disk
- **Canopy reporting** — submission outcomes reported back to Canopy after every entity
- **`--dry-run`** — print what would be submitted without calling ENA
- **`--hold-until`** — set an ENA release date (HOLD action) for projects and samples

## Installation

```bash
# From the broker-test directory:
pip install -e ".[dev]"

# Copy and fill in your credentials:
cp .env.example .env
```

## Configuration

Set the following environment variables (or put them in `.env`):

| Variable | Required | Description |
|---|---|---|
| `WEBIN_USERNAME` | Yes | ENA Webin account, e.g. `Webin-12345` |
| `WEBIN_PASSWORD` | Yes | ENA Webin password |
| `CANOPY_BASE_URL` | Yes | Base URL of the Canopy API |
| `CANOPY_USERNAME` | Yes | Username to login to Canopy API |
| `CANOPY_USERNAME` | Yes | Password to login to Canopy API |
| `ENA_BASE_URL` | No | ENA drop-box endpoint (default: prod) |
| `BROKER_STATE_DIR` | No | State file directory (default: `~/.broker/state`) |
| `BROKER_RECEIPT_DIR` | No | Receipt directory (default: `~/.broker/receipts`) |
| `HTTP_TIMEOUT_SECONDS` | No | Per-request timeout (default: `30.0`) |
| `HTTP_MAX_RETRIES` | No | Max retries on transport errors (default: `3`) |

For testing against ENA's dev server, set:
```
ENA_BASE_URL=https://wwwdev.ebi.ac.uk/ena/submit/drop-box/submit/
```

## Usage

### Bulk submission — all entity types for a taxonomy ID

```bash
broker submit ready --tax-id 9606
```

### Bulk submission — one entity type only

```bash
broker submit ready --tax-id 9606 --only projects
broker submit ready --tax-id 9606 --only samples
broker submit ready --tax-id 9606 --only experiments
broker submit ready --tax-id 9606 --only runs
```

**Note:** Partial bulk submissions must have prerequisite accessions present in the Canopy payload or supplied via CLI flags. The broker will not auto-submit dependencies.

### Bulk with hold date (release date for projects + samples)

```bash
broker submit ready --tax-id 9606 --hold-until 2026-01-01
```

### Dry run — see what would be submitted

```bash
broker submit ready --tax-id 9606 --dry-run
```

### Validation only

```bash
broker submit ready --tax-id 9606 --validate-only
```

### Targeted single-entity submission

```bash
# Project (no prerequisites)
broker submit entity --type project --id my-project-id

# Sample (project accession required if not in Canopy payload)
broker submit entity --type sample --id my-sample-id \
  --project-accession PRJEB12345

# Experiment
broker submit entity --type experiment --id my-exp-id \
  --project-accession PRJEB12345 \
  --sample-accession ERS111111

# Run
broker submit entity --type run --id my-run-id \
  --experiment-accession ERX222222
```

### Resume an interrupted attempt

```bash
broker resume --attempt-id <uuid-from-prior-attempt>
```

Resume loads state from `~/.broker/state/<attempt_id>.json`, skips already-succeeded entities, and re-submits the rest. Canopy is not re-called — payloads are read from local state.

If a prerequisite accession is now available that wasn't before, supply it:
```bash
broker resume --attempt-id <id> --project-accession PRJEB12345
```

## State and receipts

After every submission:
- Attempt state is written to `~/.broker/state/<attempt_id>.json`
- Raw ENA receipts are written to `~/.broker/receipts/<attempt_id>/<type>_<id>.txt`

To inspect a saved attempt:
```bash
cat ~/.broker/state/<attempt_id>.json | python3 -m json.tool
```

## Prerequisite rules

| Entity | Required accessions |
|---|---|
| project | none |
| sample | `project_accession` (PRJEB\*) |
| experiment | `project_accession` (PRJEB\*) + `sample_accession` (ERS\*) |
| run | `experiment_accession` (ERX\*) |

In bulk mode, accessions from already-succeeded entities in the same attempt are automatically propagated to downstream entities. In targeted and partial-scope modes, missing accessions always produce a clear error rather than triggering a hidden dependency submission.

## Architecture

```
src/broker/
  cli.py                        CLI commands (argument parsing + DI wiring)
  config.py                     Settings from env vars / .env
  enums.py                      EntityType, AttemptStatus, SubmissionMode, ...
  errors.py                     Exception hierarchy
  models/
    attempt.py                  AttemptState + EntitySubmissionState (state document)
    canopy.py                   ClaimResponse, ReportPayload, ...
    ena.py                      ENASubmissionResult, ENAAccessions, XML builders
  clients/
    canopy.py                   Canopy HTTP client (claim, validate, report)
    ena.py                      ENA Webin drop-box client (never raises)
  services/
    prerequisite_validation.py  Accession resolution; fails if missing
    transform_service.py        Canopy payload → ENA XML (via lxml)
    receipt_parser.py           ENA XML/JSON receipt → ENASubmissionResult
    submission_service.py       Single-entity lifecycle + checkpoint ordering
    report_service.py           Report outcomes to Canopy
    orchestrator.py             Bulk + targeted attempt coordination
    resume_service.py           Resume interrupted attempts
  storage/
    state_store.py              Atomic JSON state persistence
    receipt_store.py            Raw receipt storage
```

## Running tests

```bash
# All tests
pytest tests/unit tests/integration -v

# With coverage
pytest tests/unit tests/integration --cov=src/broker --cov-report=term-missing

# End-to-end (requires ENA test account + ENA_E2E=1)
ENA_E2E=1 pytest tests/ -m e2e
```

## ENA XML format

The broker uses ENA's XML drop-box endpoint. Each submission sends a multipart POST with:
- `SUBMISSION`: action XML (ADD, optional HOLD date)
- Entity XML set (`PROJECT_SET`, `SAMPLE_SET`, `EXPERIMENT_SET`, `RUN_SET`)

ENA assigns aliases in the format `broker-{type}-{id}`. Because ENA Webin is idempotent on alias, re-submitting the same alias returns the previously assigned accession — this is the safety property that makes crash-safe resume correct.

## Future: FTP upload for runs

FTP upload is not yet implemented. The architecture anticipates it:
- `ENARunXML.data_block_files` already carries filename/checksum metadata
- A future `upload_service.py` slots in as a pre-step in `SubmissionService.submit_entity()` for runs
- The run XML references already-uploaded files by filename
