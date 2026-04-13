"""Models for Canopy API request and response payloads."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from broker.enums import EntityType


class CanopyEntityRelationships(BaseModel):
    """Prerequisite accessions Canopy already knows for an entity.

    Fields present depend on entity type:
      experiments: project_accession, sample_accession (+ IDs)
      reads:       experiment_accession (+ IDs)
      projects:    organism_key, project_type
      samples:     typically absent (null)
    """

    # Experiment prerequisites
    sample_id: str | None = None
    sample_submission_id: str | None = None
    sample_accession: str | None = None
    project_accession: str | None = None

    # Read prerequisites
    experiment_id: str | None = None
    experiment_submission_id: str | None = None
    experiment_accession: str | None = None

    # Project metadata
    organism_key: str | None = None
    project_type: str | None = None


class CanopyEntity(BaseModel):
    """A single entity record returned inside a ClaimResponse."""

    id: str
    submission_id: str | None = None
    status: str | None = None
    prepared_payload: dict[str, Any]
    accession: str | None = None
    relationships: CanopyEntityRelationships | None = None


class CanopyOrganism(BaseModel):
    organism_key: str
    scientific_name: str | None = None
    tax_id: int | None = None
    culture_or_strain_id: str | None = None


class ClaimResponse(BaseModel):
    """Response from POST /broker/organisms/taxid{tax_id}/claim.

    Entities are grouped by type. The 'reads' key maps to EntityType.RUN.
    """

    attempt_id: str
    organism_key: str | None = None
    organism: CanopyOrganism | None = None
    projects: list[CanopyEntity] = Field(default_factory=list)
    samples: list[CanopyEntity] = Field(default_factory=list)
    experiments: list[CanopyEntity] = Field(default_factory=list)
    reads: list[CanopyEntity] = Field(default_factory=list)


class CanopyEntityPayload(BaseModel):
    """Internal DTO used by TransformService and SubmissionService.

    Holds the fields needed to build ENA XML from a claimed entity.
    Constructed from EntitySubmissionState (which stores raw_payload from CanopyEntity).
    """

    entity_id: str
    entity_type: EntityType
    data: dict[str, Any]
    project_accession: str | None = None
    sample_accession: str | None = None
    experiment_accession: str | None = None


class ValidationResponse(BaseModel):
    """Response from GET /broker/validate/{entity_type}/{entity_id}."""

    entity_id: str
    entity_type: EntityType
    valid: bool
    errors: list[str] = Field(default_factory=list)
    prerequisites: dict[str, str] = Field(default_factory=dict)


class ReportPayload(BaseModel):
    """Payload sent to Canopy after each entity submission outcome."""

    attempt_id: str
    entity_id: str
    entity_type: EntityType
    status: str  # "succeeded" | "failed"
    accession: str | None = None
    biosample_accession: str | None = None
    raw_receipt: str | None = None
    submission_timestamp: str | None = None  # ISO 8601
    error_message: str | None = None
