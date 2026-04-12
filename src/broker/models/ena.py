"""Models for ENA submission results and XML construction helpers.

ENASubmissionResult is the return type of every ENAClient method.
ENAClient never raises on ENA HTTP errors — it always returns this object
with success=False and the raw receipt captured verbatim.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ENAAccessions(BaseModel):
    """Accession numbers extracted from an ENA receipt."""

    primary_accession: str  # PRJEB*, ERS*, ERX*, or ERR*
    biosample_accession: str | None = None  # SAMEAxxx — samples only
    alias: str | None = None  # the alias submitted, for correlation


class ENASubmissionResult(BaseModel):
    """The parsed outcome of a single ENA entity submission call.

    ENAClient always returns this — it never raises on ENA API failures.
    Callers check result.success before proceeding.
    """

    entity_id: str
    success: bool
    accessions: ENAAccessions | None = None
    raw_receipt: str = ""  # verbatim response body — XML or JSON, exactly as received
    error_message: str | None = None
    http_status: int | None = None


# ---------------------------------------------------------------------------
# XML construction input models
# Used by TransformService to carry typed data before lxml element building.
# ---------------------------------------------------------------------------


class ENARunFile(BaseModel):
    filename: str
    filetype: str  # fastq, bam, cram, etc.
    checksum: str
    checksum_method: str = "MD5"


class ENAProjectXML(BaseModel):
    alias: str
    title: str
    description: str
    # center_name comes from Webin username in config; not set here.


class ENASampleXML(BaseModel):
    alias: str
    title: str
    tax_id: str
    scientific_name: str
    sample_attributes: list[dict[str, str]] = Field(default_factory=list)


class ENAExperimentXML(BaseModel):
    alias: str
    title: str
    study_ref: str  # PRJEB accession (or alias if accession not yet assigned)
    sample_ref: str  # ERS accession (or alias)
    library_strategy: str
    library_source: str
    library_selection: str
    library_layout: str  # "SINGLE" or "PAIRED"
    platform: str  # e.g. "ILLUMINA"
    instrument_model: str  # e.g. "Illumina HiSeq 2500"


class ENARunXML(BaseModel):
    alias: str
    experiment_ref: str  # ERX accession (or alias)
    # FTP upload not yet implemented. Files are referenced here once uploaded.
    # Future: add a pre-step in SubmissionService that uploads via FTP and
    # populates this list before the XML submission call.
    data_block_files: list[ENARunFile] = Field(default_factory=list)
