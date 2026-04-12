"""ENA submission broker CLI.

Commands:
  broker submit ready --tax-id <id>         bulk submission by taxonomy ID
  broker submit entity --type <t> --id <id> targeted single-entity submission
  broker resume --attempt-id <id>           resume an interrupted attempt

The CLI's responsibility is:
  1. Parse and validate arguments
  2. Load settings from env / .env file
  3. Wire up all clients and services (manual DI)
  4. Call the appropriate orchestrator method
  5. Render results and exit with an appropriate code

All business logic lives in services. The CLI catches BrokerError subclasses
and renders them as clear error messages with non-zero exit codes.
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from broker.enums import EntityType, SubmissionMode

app = typer.Typer(
    name="broker",
    help="ENA submission broker — submits genomic metadata to ENA on behalf of Canopy.",
    no_args_is_help=True,
    add_completion=False,
)
submit_app = typer.Typer(help="Submission commands.", no_args_is_help=True)
app.add_typer(submit_app, name="submit")

console = Console()
err_console = Console(stderr=True, style="bold red")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


# ---------------------------------------------------------------------------
# broker submit ready
# ---------------------------------------------------------------------------


@submit_app.command("ready")
def submit_ready(
    tax_id: Annotated[str, typer.Option("--tax-id", help="Taxonomy ID (e.g. 9606)")],
    only: Annotated[
        Optional[str],
        typer.Option(
            "--only",
            help="Submit only this entity type: projects | samples | experiments | runs",
        ),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print what would be submitted, without calling ENA")] = False,
    validate_only: Annotated[bool, typer.Option("--validate-only", help="Run Canopy validation checks only; do not submit")] = False,
    hold_until: Annotated[
        Optional[str],
        typer.Option(
            "--hold-until",
            help="ENA release date in ISO 8601 format (e.g. 2026-01-01). Applied to projects and samples.",
        ),
    ] = None,
    # Accession overrides (used when --only is set without full dependency chain)
    project_accession: Annotated[Optional[str], typer.Option("--project-accession", help="Project/study accession (PRJEB*)")] = None,
    sample_accession: Annotated[Optional[str], typer.Option("--sample-accession", help="Sample accession (ERS*)")] = None,
    experiment_accession: Annotated[Optional[str], typer.Option("--experiment-accession", help="Experiment accession (ERX*)")] = None,
) -> None:
    """Submit all ready entities for a taxonomy ID, in dependency order."""
    from broker.errors import BrokerError

    _validate_hold_until(hold_until)

    only_type: EntityType | None = None
    if only is not None:
        only_type = _parse_entity_type_plural(only)

    submission_mode = _resolve_submission_mode(dry_run, validate_only)
    cli_overrides = _build_cli_overrides(project_accession, sample_accession, experiment_accession)

    try:
        orchestrator = _build_orchestrator(submission_mode=submission_mode)
        attempt = orchestrator.run_bulk(
            tax_id=tax_id,
            only=only_type,
            submission_mode=submission_mode,
            cli_overrides=cli_overrides,
            hold_until_date=hold_until,
        )
        _render_attempt(attempt)
        if attempt.status.value in ("failed", "partial"):
            raise typer.Exit(code=1)
    except BrokerError as exc:
        err_console.print(f"[ERROR] {exc}")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# broker submit entity
# ---------------------------------------------------------------------------


@submit_app.command("entity")
def submit_entity(
    type_: Annotated[str, typer.Option("--type", help="Entity type: project | sample | experiment | run")],
    id_: Annotated[str, typer.Option("--id", help="Entity ID in Canopy")],
    project_accession: Annotated[Optional[str], typer.Option("--project-accession", help="Project accession (PRJEB*)")] = None,
    sample_accession: Annotated[Optional[str], typer.Option("--sample-accession", help="Sample accession (ERS*)")] = None,
    experiment_accession: Annotated[Optional[str], typer.Option("--experiment-accession", help="Experiment accession (ERX*)")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    hold_until: Annotated[
        Optional[str],
        typer.Option("--hold-until", help="ENA release date ISO 8601 (e.g. 2026-01-01). For projects and samples only."),
    ] = None,
) -> None:
    """Submit a single entity by type and ID.

    Prerequisites (project/sample/experiment accessions) must be supplied
    via flags or present in the Canopy payload. The broker will not
    auto-submit dependencies — missing prerequisites produce a clear error.
    """
    from broker.errors import BrokerError

    _validate_hold_until(hold_until)
    entity_type = _parse_entity_type(type_)
    submission_mode = _resolve_submission_mode(dry_run, False)
    cli_overrides = _build_cli_overrides(project_accession, sample_accession, experiment_accession)

    try:
        orchestrator = _build_orchestrator(submission_mode=submission_mode)
        attempt = orchestrator.run_targeted(
            entity_type=entity_type,
            entity_id=id_,
            cli_overrides=cli_overrides,
            submission_mode=submission_mode,
            hold_until_date=hold_until,
        )
        _render_attempt(attempt)
        if attempt.status.value in ("failed", "partial"):
            raise typer.Exit(code=1)
    except BrokerError as exc:
        err_console.print(f"[ERROR] {exc}")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# broker resume
# ---------------------------------------------------------------------------


@app.command("resume")
def resume(
    attempt_id: Annotated[str, typer.Option("--attempt-id", help="Attempt ID to resume")],
    project_accession: Annotated[Optional[str], typer.Option("--project-accession")] = None,
    sample_accession: Annotated[Optional[str], typer.Option("--sample-accession")] = None,
    experiment_accession: Annotated[Optional[str], typer.Option("--experiment-accession")] = None,
) -> None:
    """Resume a previously interrupted submission attempt.

    Loads persisted attempt state, skips entities already succeeded,
    and re-submits the rest. Canopy is not re-called — payloads are
    read from local state. The original hold-until date is preserved.
    """
    from broker.errors import BrokerError

    cli_overrides = _build_cli_overrides(project_accession, sample_accession, experiment_accession)

    try:
        resume_svc = _build_resume_service()
        attempt = resume_svc.resume(attempt_id, cli_overrides=cli_overrides)
        _render_attempt(attempt)
        if attempt.status.value in ("failed", "partial"):
            raise typer.Exit(code=1)
    except BrokerError as exc:
        err_console.print(f"[ERROR] {exc}")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Wiring — manual dependency injection
# ---------------------------------------------------------------------------


def _build_orchestrator(submission_mode: SubmissionMode):
    """Wire all clients and services and return an Orchestrator."""
    from broker.clients.canopy import CanopyClient
    from broker.clients.ena import ENAClient
    from broker.config import get_settings
    from broker.services.orchestrator import Orchestrator
    from broker.services.prerequisite_validation import PrerequisiteValidator
    from broker.services.receipt_parser import ReceiptParser
    from broker.services.report_service import ReportService
    from broker.services.submission_service import SubmissionService
    from broker.services.transform_service import TransformService
    from broker.storage.receipt_store import ReceiptStore
    from broker.storage.state_store import StateStore

    settings = get_settings()

    canopy_client = CanopyClient(settings)
    ena_client = ENAClient(settings)

    state_store = StateStore(settings.state_dir)
    receipt_store = ReceiptStore(settings.receipt_dir)

    transform_service = TransformService(webin_account=settings.webin_username)
    receipt_parser = ReceiptParser()
    prereq_validator = PrerequisiteValidator()
    report_service = ReportService(canopy_client)

    submission_service = SubmissionService(
        ena_client=ena_client,
        transform_service=transform_service,
        prerequisite_validator=prereq_validator,
        receipt_parser=receipt_parser,
        state_store=state_store,
        receipt_store=receipt_store,
        report_service=report_service,
    )

    return Orchestrator(
        canopy_client=canopy_client,
        submission_service=submission_service,
        state_store=state_store,
    )


def _build_resume_service():
    from broker.clients.canopy import CanopyClient
    from broker.clients.ena import ENAClient
    from broker.config import get_settings
    from broker.services.prerequisite_validation import PrerequisiteValidator
    from broker.services.receipt_parser import ReceiptParser
    from broker.services.report_service import ReportService
    from broker.services.resume_service import ResumeService
    from broker.services.submission_service import SubmissionService
    from broker.services.transform_service import TransformService
    from broker.storage.receipt_store import ReceiptStore
    from broker.storage.state_store import StateStore

    settings = get_settings()

    canopy_client = CanopyClient(settings)
    ena_client = ENAClient(settings)

    state_store = StateStore(settings.state_dir)
    receipt_store = ReceiptStore(settings.receipt_dir)

    transform_service = TransformService(webin_account=settings.webin_username)
    receipt_parser = ReceiptParser()
    prereq_validator = PrerequisiteValidator()
    report_service = ReportService(canopy_client)

    submission_service = SubmissionService(
        ena_client=ena_client,
        transform_service=transform_service,
        prerequisite_validator=prereq_validator,
        receipt_parser=receipt_parser,
        state_store=state_store,
        receipt_store=receipt_store,
        report_service=report_service,
    )

    return ResumeService(state_store=state_store, submission_service=submission_service)


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


def _render_attempt(attempt) -> None:
    """Render submission results as a Rich table."""
    from broker.enums import EntitySubmissionStatus

    table = Table(title=f"Attempt {attempt.attempt_id} — {attempt.status.upper()}")
    table.add_column("Type", style="cyan")
    table.add_column("ID")
    table.add_column("Status")
    table.add_column("Accession", style="green")
    table.add_column("BioSample", style="green")
    table.add_column("Error", style="red")

    status_styles = {
        EntitySubmissionStatus.SUCCEEDED: "bold green",
        EntitySubmissionStatus.FAILED: "bold red",
        EntitySubmissionStatus.SKIPPED: "dim",
        EntitySubmissionStatus.SUBMITTED: "yellow",
        EntitySubmissionStatus.PENDING: "dim",
    }

    for entity in attempt.all_entities_flat():
        style = status_styles.get(entity.status, "")
        table.add_row(
            str(entity.entity_type),
            entity.entity_id,
            f"[{style}]{entity.status}[/{style}]",
            entity.ena_accession or "",
            entity.biosample_accession or "",
            (entity.error_message or "")[:80],
        )

    console.print(table)
    console.print(f"State saved to: ~/.broker/state/{attempt.attempt_id}.json")


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------


def _parse_entity_type(value: str) -> EntityType:
    try:
        return EntityType(value.lower())
    except ValueError:
        valid = ", ".join(et.value for et in EntityType)
        err_console.print(f"[ERROR] Invalid entity type '{value}'. Valid types: {valid}")
        raise typer.Exit(code=1)


def _parse_entity_type_plural(value: str) -> EntityType:
    """Accept plural form (projects, samples, etc.) and map to EntityType."""
    mapping = {
        "projects": EntityType.PROJECT,
        "samples": EntityType.SAMPLE,
        "experiments": EntityType.EXPERIMENT,
        "runs": EntityType.RUN,
    }
    result = mapping.get(value.lower())
    if result is None:
        valid = ", ".join(mapping.keys())
        err_console.print(f"[ERROR] Invalid --only value '{value}'. Valid values: {valid}")
        raise typer.Exit(code=1)
    return result


def _resolve_submission_mode(dry_run: bool, validate_only: bool) -> SubmissionMode:
    if dry_run and validate_only:
        err_console.print("[ERROR] --dry-run and --validate-only are mutually exclusive.")
        raise typer.Exit(code=1)
    if dry_run:
        return SubmissionMode.DRY_RUN
    if validate_only:
        return SubmissionMode.VALIDATE_ONLY
    return SubmissionMode.NORMAL


def _build_cli_overrides(
    project_accession: str | None,
    sample_accession: str | None,
    experiment_accession: str | None,
) -> dict[str, str]:
    overrides: dict[str, str] = {}
    if project_accession:
        overrides["project_accession"] = project_accession
    if sample_accession:
        overrides["sample_accession"] = sample_accession
    if experiment_accession:
        overrides["experiment_accession"] = experiment_accession
    return overrides


def _validate_hold_until(hold_until: str | None) -> None:
    if hold_until is None:
        return
    import re
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", hold_until):
        err_console.print(
            f"[ERROR] --hold-until '{hold_until}' is not a valid ISO 8601 date. "
            f"Use the format YYYY-MM-DD, e.g. 2026-01-01"
        )
        raise typer.Exit(code=1)
