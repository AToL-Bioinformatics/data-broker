"""ENA submission broker CLI.

Commands:
  broker submit ready  --tax-id <id>               bulk submission by taxonomy ID
  broker submit entity --type <t> --id <id>         targeted single-entity submission
  broker submit batch  --samples id1,id2 ...        submit specific entities by ID
                       --from-file batch.json
  broker resume        --attempt-id <id>            resume an interrupted attempt

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

import json
import logging
import re
import sys
from pathlib import Path
from typing import Annotated, List, Optional

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
tolid_app = typer.Typer(help="Tree of Life ID (ToLID) commands.", no_args_is_help=True)
app.add_typer(submit_app, name="submit")
app.add_typer(tolid_app, name="tolid")

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
# broker submit batch
# ---------------------------------------------------------------------------


@submit_app.command("batch")
def submit_batch(
    projects: Annotated[
        Optional[str],
        typer.Option("--projects", help="Comma-separated project IDs, e.g. uuid1,uuid2"),
    ] = None,
    samples: Annotated[
        Optional[str],
        typer.Option("--samples", help="Comma-separated sample IDs"),
    ] = None,
    experiments: Annotated[
        Optional[str],
        typer.Option("--experiments", help="Comma-separated experiment IDs"),
    ] = None,
    runs: Annotated[
        Optional[str],
        typer.Option("--runs", help="Comma-separated run IDs"),
    ] = None,
    from_file: Annotated[
        Optional[Path],
        typer.Option(
            "--from-file",
            help=(
                'JSON file with entity IDs, e.g. {"samples": ["id1","id2"], "experiments": ["id3"]}. '
                "Merged with any inline --samples/--experiments/etc flags."
            ),
            exists=True,
            file_okay=True,
            dir_okay=False,
        ),
    ] = None,
    project_accession: Annotated[Optional[str], typer.Option("--project-accession", help="Project accession (PRJEB*)")] = None,
    sample_accession: Annotated[Optional[str], typer.Option("--sample-accession", help="Sample accession (ERS*)")] = None,
    experiment_accession: Annotated[Optional[str], typer.Option("--experiment-accession", help="Experiment accession (ERX*)")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    hold_until: Annotated[
        Optional[str],
        typer.Option("--hold-until", help="ENA release date ISO 8601 (e.g. 2026-01-01). For projects and samples only."),
    ] = None,
) -> None:
    """Submit specific entities by ID, across one or more entity types.

    IDs can be supplied inline (comma-separated) or via a JSON file.

    \b
    Examples:
      # Inline — comma-separated per type
      broker submit batch --samples abc-123,def-456 --experiments xyz-789

      # From a JSON file
      broker submit batch --from-file batch.json

      # Combined — file provides base, inline flags add more
      broker submit batch --from-file batch.json --runs extra-run-id

    \b
    JSON file format:
      {
        "projects":    ["uuid", ...],
        "samples":     ["uuid", ...],
        "experiments": ["uuid", ...],
        "runs":        ["uuid", ...]
      }

    Prerequisites must be supplied via flags or present in the Canopy payload.
    The broker will not auto-submit dependencies.
    """
    from broker.errors import BrokerError

    _validate_hold_until(hold_until)
    submission_mode = _resolve_submission_mode(dry_run, False)
    cli_overrides = _build_cli_overrides(project_accession, sample_accession, experiment_accession)

    # Merge --from-file with inline flags
    project_ids = _parse_id_csv(projects)
    sample_ids = _parse_id_csv(samples)
    experiment_ids = _parse_id_csv(experiments)
    run_ids = _parse_id_csv(runs)

    if from_file is not None:
        file_ids = _load_batch_file(from_file)
        project_ids = list({*project_ids, *file_ids.get("projects", [])})
        sample_ids = list({*sample_ids, *file_ids.get("samples", [])})
        experiment_ids = list({*experiment_ids, *file_ids.get("experiments", [])})
        run_ids = list({*run_ids, *file_ids.get("runs", [])})

    if not any([project_ids, sample_ids, experiment_ids, run_ids]):
        err_console.print("[ERROR] No entity IDs provided. Use --samples, --experiments, etc. or --from-file.")
        raise typer.Exit(code=1)

    try:
        orchestrator = _build_orchestrator(submission_mode=submission_mode)
        attempt = orchestrator.run_batch(
            project_ids=project_ids or None,
            sample_ids=sample_ids or None,
            experiment_ids=experiment_ids or None,
            run_ids=run_ids or None,
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
# broker tolid request
# ---------------------------------------------------------------------------


@tolid_app.command("request")
def tolid_request(
    tax_id: Annotated[
        Optional[str],
        typer.Option("--tax-id", help="Optional taxon filter for requestable ToLIDs"),
    ] = None,
    sample_id: Annotated[
        Optional[str],
        typer.Option("--sample-id", help="Optional Canopy sample ID to process"),
    ] = None,
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", help="Optional maximum number of ToLIDs to request"),
    ] = None,
    update_ena: Annotated[
        bool,
        typer.Option(
            "--update-ena/--no-update-ena",
            help=(
                "Submit a MODIFY to ENA after each successful ToLID request "
                "to record the tolid sample attribute (default: on)."
            ),
        ),
    ] = True,
) -> None:
    """Request Tree of Life IDs for Canopy rows in `not_requested` state."""
    from broker.errors import BrokerError

    try:
        tolid_svc = _build_tolid_service()
        results = tolid_svc.process_requestable(
            tax_id=tax_id,
            sample_id=sample_id,
            limit=limit,
            update_ena=update_ena,
        )

        _render_tolid_results(results, "requestable", update_ena)

        if any(r.error for r in results):
            raise typer.Exit(code=1)

    except BrokerError as exc:
        err_console.print(f"[ERROR] {exc}")
        raise typer.Exit(code=1)


@tolid_app.command("poll")
def tolid_poll(
    tax_id: Annotated[
        Optional[str],
        typer.Option("--tax-id", help="Optional taxon filter for pending ToLIDs"),
    ] = None,
    sample_id: Annotated[
        Optional[str],
        typer.Option("--sample-id", help="Optional Canopy sample ID to poll"),
    ] = None,
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", help="Optional maximum number of pending ToLIDs to poll"),
    ] = None,
    retry_after_hours: Annotated[
        Optional[float],
        typer.Option(
            "--retry-after-hours",
            help="Minimum age in hours before retrying a pending ToLID",
        ),
    ] = None,
    update_ena: Annotated[
        bool,
        typer.Option(
            "--update-ena/--no-update-ena",
            help=(
                "Submit a MODIFY to ENA after each successful ToLID request "
                "to record the tolid sample attribute (default: on)."
            ),
        ),
    ] = True,
) -> None:
    """Retry Tree of Life IDs for Canopy rows in `pending` state."""
    from broker.config import get_settings
    from broker.errors import BrokerError

    try:
        settings = get_settings()
        tolid_svc = _build_tolid_service(
            retry_after_hours=retry_after_hours or settings.tolid_retry_after_hours
        )
        results = tolid_svc.process_pending(
            tax_id=tax_id,
            sample_id=sample_id,
            limit=limit,
            update_ena=update_ena,
        )

        _render_tolid_results(results, "pending", update_ena)

        if any(r.error for r in results):
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
    )

    return Orchestrator(
        canopy_client=canopy_client,
        submission_service=submission_service,
        state_store=state_store,
        report_service=report_service,
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
    )

    return ResumeService(
        state_store=state_store,
        submission_service=submission_service,
        canopy_client=canopy_client,
        report_service=report_service,
    )


def _build_tolid_service(retry_after_hours: float | None = None):
    from broker.clients.canopy import CanopyClient
    from broker.clients.ena import ENAClient
    from broker.clients.tolid import ToLIDClient
    from broker.config import get_settings
    from broker.errors import ConfigurationError
    from broker.services.tolid_service import ToLIDService
    from broker.services.transform_service import TransformService

    settings = get_settings()

    if not settings.tolid_api_key:
        raise ConfigurationError(
            "TOLID_API_KEY is not set. "
            "Set it in your .env file or environment to use ToLID features."
        )

    return ToLIDService(
        canopy_client=CanopyClient(settings),
        tolid_client=ToLIDClient(
            api_key=settings.tolid_api_key,
            base_url=settings.tolid_base_url,
        ),
        ena_client=ENAClient(settings),
        transform_service=TransformService(webin_account=settings.webin_username),
        retry_after_hours=retry_after_hours or settings.tolid_retry_after_hours,
    )


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
    table.add_column("ToLID", style="green")
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
            entity.tolid or "",
            (entity.error_message or "")[:80],
        )

    console.print(table)
    console.print(f"State saved to: ~/.broker/state/{attempt.attempt_id}.json")


def _render_tolid_results(results, scope: str, update_ena: bool) -> None:
    """Render ToLID request outcomes as a Rich table."""
    from broker.services.tolid_service import ToLIDResult

    table = Table(title=f"ToLID results — {scope}")
    table.add_column("Sample ID", style="cyan")
    table.add_column("Specimen ID", style="green")
    table.add_column("Status")
    table.add_column("ToLID", style="green")
    if update_ena:
        table.add_column("ENA Updated")
    table.add_column("Notes", style="dim")

    for r in results:
        if r.skipped:
            row = [r.sample_id, r.specimen_id, str(r.status), r.tolid or ""]
            if update_ena:
                row.append("")
            row.append(r.note or "skipped")
            table.add_row(*row, style="dim")
        elif r.error:
            row = [r.sample_id, r.specimen_id, str(r.status), ""]
            if update_ena:
                row.append("[red]no[/red]")
            row.append(r.error)
            table.add_row(*row)
        else:
            row = [r.sample_id, r.specimen_id, str(r.status), r.tolid or ""]
            if update_ena:
                if r.status == "assigned":
                    row.append("[green]yes[/green]" if r.ena_updated else "[yellow]failed[/yellow]")
                else:
                    row.append("")
            notes = r.note or ""
            if r.request_id:
                notes = f"{notes} request_id={r.request_id}".strip()
            row.append(notes)
            table.add_row(*row)

    console.print(table)


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
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", hold_until):
        err_console.print(
            f"[ERROR] --hold-until '{hold_until}' is not a valid ISO 8601 date. "
            f"Use the format YYYY-MM-DD, e.g. 2026-01-01"
        )
        raise typer.Exit(code=1)


def _parse_id_csv(value: str | None) -> list[str]:
    """Parse a comma- or whitespace-separated string of IDs into a list.

    Accepts:  "abc-123,def-456"  or  "abc-123 def-456"  or  "abc-123, def-456"
    Returns:  ["abc-123", "def-456"]
    """
    if not value:
        return []
    return [v.strip() for v in re.split(r"[,\s]+", value) if v.strip()]


def _load_batch_file(path: Path) -> dict[str, list[str]]:
    """Load a batch JSON file and return a dict of entity_type → list[id].

    Expected format:
      {"projects": ["uuid1"], "samples": ["uuid2", "uuid3"], ...}
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        err_console.print(f"[ERROR] Could not read batch file '{path}': {exc}")
        raise typer.Exit(code=1)
    if not isinstance(data, dict):
        err_console.print(f"[ERROR] Batch file must be a JSON object, got {type(data).__name__}")
        raise typer.Exit(code=1)
    valid_keys = {"projects", "samples", "experiments", "runs"}
    unknown = set(data) - valid_keys
    if unknown:
        err_console.print(f"[ERROR] Unknown keys in batch file: {sorted(unknown)}. Valid keys: {sorted(valid_keys)}")
        raise typer.Exit(code=1)
    return {k: list(v) for k, v in data.items() if isinstance(v, list)}
