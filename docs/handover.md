# Broker Handover

## Purpose

The broker is a Python CLI that:

- claims submission-ready metadata from Canopy
- transforms it into ENA XML
- submits it to ENA Webin
- stores local state and raw ENA receipts
- reports outcomes back to Canopy
- supports a separate ToLID workflow for samples

It is not a generic ETL app and it does not upload raw read files itself.

Use [README.md](../README.md) for:

- installation and configuration
- CLI command reference
- prerequisite accessions
- hold-date behaviour
- state and receipts

## What It Does

At a high level the broker:

1. Claims entities from Canopy.
2. Submits them to ENA in dependency order.
3. Stores local attempt state and raw ENA receipts.
4. Reports outcomes back to Canopy.
5. Handles a separate ToLID request/poll workflow for samples.

## Boundaries

The broker does:

- metadata submission to ENA
- local crash-safe attempt persistence
- ENA receipt capture
- reporting results back to Canopy
- ToLID request/poll work

The broker does not do:

- raw data file transfer to ENA
- automatic dependency submission outside the claimed scope
- durable ToLID state management outside Canopy
- assembly submission workflow orchestration

Operationally important:

- Do not report results to an AWS/prod Canopy deployment if you submitted against ENA dev/staging.
- Run submission assumes the raw data files are already transferred to ENA.
- Raw data transfer to ENA happens separately and must be complete before `run` submissions can succeed.
- The genome launcher workflow handles raw data transfer to ENA and assembly submission separately from this broker.

## Architecture

High-level shape:

```text
CLI (Typer)
  -> Orchestrator / ResumeService / ToLIDService
    -> CanopyClient / ENAClient / ToLIDClient
    -> TransformService / PrerequisiteValidator / ReportService
    -> StateStore / ReceiptStore
```

Design points to note:

- The CLI is thin. Business logic lives in `services/`.
- Submission state is persisted after each checkpoint.
- Resume does not re-claim from Canopy; it reuses the saved raw payloads.
- ENA submission failures are returned as results, not raised, so state can still be saved.
- Reporting to Canopy happens after the run in a `finally` path.

## Codebase Map

Core areas:

- `src/broker/cli.py`: CLI commands and dependency wiring.
- `src/broker/services/orchestrator.py`: bulk, targeted, and batch submission flow.
- `src/broker/services/submission_service.py`: per-entity submission lifecycle.
- `src/broker/services/resume_service.py`: resume interrupted attempts.
- `src/broker/services/report_service.py`: batch-report outcomes to Canopy.
- `src/broker/services/transform_service.py`: Generates ENA XML from canopy payload
- `src/broker/services/prerequisite_validation.py`: prerequisite accession resolution.
- `src/broker/services/tolid_service.py`: ToLID request/poll workflow.
- `src/broker/clients/canopy.py`: Canopy auth, claims, reporting, ToLID broker endpoints.
- `src/broker/clients/ena.py`: ENA Webin submission client.
- `src/broker/clients/tolid.py`: Sanger ToLID client.
- `src/broker/storage/state_store.py`: persisted attempt JSON.
- `src/broker/storage/receipt_store.py`: raw ENA receipts.
- `src/broker/models/`: request/response and state models.
- `tests/`: unit and integration coverage for the main logic.

## Important Flows

### Submission flow

- The detailed operator flow and command semantics are in [README.md](../README.md).
- For engineering purposes, the key distinction is that bulk submissions can resolve some prerequisites from earlier succeeded entities in the same attempt, while targeted and partial modes cannot.

Prerequisite resolution order:

1. CLI override flags
2. prerequisites already present in the Canopy claim payload
3. succeeded entities in the same attempt, but only in full bulk mode
4. fail clearly

### State and receipts

- Paths and operator usage are documented in [README.md](../README.md).
- State writes are atomic.
- `SUBMITTED` is a crash-safety checkpoint, not a final state.

### ToLID flow

- ToLID work is separate from the main submission lifecycle.
- Canopy is the source of truth for ToLID rows, tracks any TOLID requests which need to be resolved.
- `--update-ena` sends an ENA `MODIFY` only after a ToLID is assigned.

Operator-facing examples and checks live in [runbook.md](runbook.md).

## Environment And Credentials

The full environment variable list is in [README.md](../README.md).

Environment rules that matter for handover:

- Default broker behaviour uses ENA dev/staging unless `--prod` is passed.
- The ToLID client also defaults to staging unless `--prod` is passed.

## Caveats And Future Work TODO

Things already visible in code:

- `--validate-only` is accepted by the CLI but not fully implemented as a true no-submit path.
- Run XML assumes files already exist in ENA-accessible storage.
- Experiment XML defaults some missing library fields to sentinel values; that may keep submissions valid but is not a data-quality substitute.

