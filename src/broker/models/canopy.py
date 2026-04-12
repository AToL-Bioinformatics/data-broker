"""Models for Canopy API request and response payloads.

ASSUMPTION: Canopy API paths and shapes are inferred from the spec.
They are isolated here and in clients/canopy.py so they can be updated
without touching any other layer.

Claim endpoints:
  POST /claim                    bulk claim by tax_id
  POST /claim/entity             targeted claim by type + id

Validate endpoint:
  GET  /validate/{type}/{id}

Report endpoint:
  POST /report
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from broker.enums import EntityType


class CanopyEntityPayload(BaseModel):
    """A single entity record returned by Canopy's claim response.

    Prerequisite accession fields (project_accession, sample_accession,
    experiment_accession) are OPTIONAL. Canopy may or may not include them.
    Their absence must produce a clear validation error — not a silent assumption.
    """

    entity_id: str
    entity_type: EntityType
    data: dict[str, Any]  # full entity data used by TransformService

    # Prerequisites Canopy already knows — may be absent, always check.
    project_accession: str | None = None
    sample_accession: str | None = None
    experiment_accession: str | None = None


class ClaimResponse(BaseModel):
    """Response from POST /claim or POST /claim/entity."""

    attempt_id: str
    entities: list[CanopyEntityPayload] = Field(default_factory=list)


class ValidationResponse(BaseModel):
    """Response from GET /validate/{entity_type}/{entity_id}."""

    entity_id: str
    entity_type: EntityType
    valid: bool
    errors: list[str] = Field(default_factory=list)
    # Prerequisites Canopy resolved server-side (may be empty)
    prerequisites: dict[str, str] = Field(default_factory=dict)


class ReportPayload(BaseModel):
    """Payload sent to Canopy after each entity submission outcome (POST /report)."""

    attempt_id: str
    entity_id: str
    entity_type: EntityType
    status: str  # "succeeded" | "failed"
    accession: str | None = None
    biosample_accession: str | None = None
    raw_receipt: str | None = None  # raw ENA response body, verbatim
    submission_timestamp: str | None = None  # ISO 8601
    error_message: str | None = None
