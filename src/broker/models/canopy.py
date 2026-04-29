"""Models for Canopy API request and response payloads.

Base path: /api/v1/broker  (set via CANOPY_BASE_URL env var)

Endpoints:
  POST /broker/claims/ready      bulk claim by tax_id
  POST /broker/claims/entity     targeted claim by type + id
  POST /broker/claims/batch      claim multiple specific entities
  POST /broker/validation        validate prerequisites (POST, not GET)
  POST /broker/reports/{id}      report submission outcomes (batch, attempt_id path-only)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from broker.enums import EntityType


# ---------------------------------------------------------------------------
# Claim response models
# ---------------------------------------------------------------------------


class CanopyEntityPrerequisites(BaseModel):
    """Prerequisite accession state returned inside a ClaimResponse entity.

    Resolved fields — accessions Canopy already knows from prior submissions:
      project_accession, sample_accession,
      experiment_accession, run_accession

    Required fields — what THIS entity needs before it can be submitted:
      required_project_accession  ← canonical "needs a project" field
      required_sample_accession
      required_experiment_accession
      required_run_accession

    """

    # Already-resolved accessions
    project_accession: str | None = None
    sample_accession: str | None = None
    experiment_accession: str | None = None
    run_accession: str | None = None

    # What this entity requires
    required_project_accession: str | None = None
    required_sample_accession: str | None = None
    required_experiment_accession: str | None = None
    required_run_accession: str | None = None


class CanopyEntityFile(BaseModel):
    filename: str
    filetype: str


class CanopyEntity(BaseModel):
    """A single entity record inside a ClaimResponse.

    Entity type uses canonical broker names: project | sample | experiment | run
    (the backend stores 'run' as read/read_submission internally).
    """

    type: EntityType
    id: str
    tax_id: str | None = None
    scientific_name: str | None = None
    payload: dict[str, Any]
    prerequisites: CanopyEntityPrerequisites | None = None
    files: list[CanopyEntityFile] = Field(default_factory=list)

    @field_validator("files", mode="before")
    @classmethod
    def _coerce_null_files(cls, v: object) -> object:
        """Server may return ``"files": null`` — treat it as an empty list."""
        return v if v is not None else []


class ClaimResponse(BaseModel):
    """Response from any POST /broker/claims/* endpoint.

    attempt_id is None when the organism exists but has no claimable entities
    — callers must handle this case (no-op, nothing to submit).
    """

    attempt_id: str | None
    tax_id: str | None = None
    scope: str | None = None
    entities: list[CanopyEntity] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal DTO: TransformService / SubmissionService
# ---------------------------------------------------------------------------


class CanopyEntityPayload(BaseModel):
    """Internal DTO bridging EntitySubmissionState → TransformService.

    Not a Canopy API model — constructed locally from the stored raw_payload.
    """

    entity_id: str
    entity_type: EntityType
    data: dict[str, Any]
    project_accession: str | None = None
    sample_accession: str | None = None
    experiment_accession: str | None = None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    field: str
    message: str


class ValidationResponse(BaseModel):
    """Response from POST /broker/validation."""

    entity_type: EntityType
    entity_id: str
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    resolved_prerequisites: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class ReportResult(BaseModel):
    """A single entity outcome sent inside a ReportBatchPayload.

    status must be one of: "accepted" | "rejected" | "submitting"
    secondary_accession is used for BioSample (SAMEA...) — NOT biosample_accession.
    response_payload should contain the full upstream ENA response for traceability.
    tolid is the Tree of Life ID assigned after a successful ToLID request (samples only).
    """

    entity_type: EntityType
    entity_id: str
    status: str  # "accepted" | "rejected" | "submitting"
    accession: str | None = None
    secondary_accession: str | None = None
    receipt_path: str | None = None
    message: str | None = None
    errors: list[str] = Field(default_factory=list)
    response_payload: dict[str, Any] | None = None
    tolid: str | None = None


class ReportBatchPayload(BaseModel):
    """Payload for POST /broker/reports/{attempt_id}.

    attempt_id goes in the URL path only — not in this body.
    """

    tax_id: str | None = None
    results: list[ReportResult]
