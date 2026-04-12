"""Transform Canopy entity payloads into ENA XML submission documents.

All XML is built with lxml.etree — never via f-strings or string concatenation —
to prevent XML injection through payload values that contain XML metacharacters.

ENA XML structure reference:
  https://ena-docs.readthedocs.io/en/latest/submit/general-guide/programmatic.html

Submission envelope:
  ENA's drop-box endpoint accepts a multipart POST with two parts:
    SUBMISSION: <SUBMISSION> ... </SUBMISSION>
    {ENTITY_SET}: the entity XML

  The SUBMISSION XML specifies the action (ADD) and, optionally, a HOLD date.

ASSUMPTION: Canopy payloads supply the following fields per entity type.
  Missing optional fields produce XML without that element.
  Missing required fields raise TransformError.

  project:    data.title, data.description
  sample:     data.title, data.tax_id, data.scientific_name, data.sample_attributes (list)
  experiment: data.title, data.library_strategy, data.library_source,
              data.library_selection, data.library_layout, data.platform,
              data.instrument_model
              + project_accession, sample_accession (resolved prerequisites)
  run:        data.files (list of {filename, filetype, checksum, checksum_method})
              + experiment_accession (resolved prerequisite)
"""

from __future__ import annotations

from lxml import etree

from broker.models.canopy import CanopyEntityPayload
from broker.errors import BrokerError


class TransformError(BrokerError):
    """A Canopy payload is missing a field required to build ENA XML."""


class TransformService:
    """Converts Canopy entity payloads to ENA XML strings."""

    def __init__(self, webin_account: str) -> None:
        # center_name in ENA XML — typically the Webin account identifier.
        self._center_name = webin_account

    # ------------------------------------------------------------------
    # Public: per-entity transform
    # ------------------------------------------------------------------

    def to_project_xml(self, payload: CanopyEntityPayload) -> str:
        """Build PROJECT_SET XML from a Canopy project payload."""
        data = payload.data
        alias = self._alias("project", payload.entity_id)
        project = etree.Element("PROJECT", alias=alias, center_name=self._center_name)
        self._required_sub(project, "TITLE", data, "title", payload)
        self._required_sub(project, "DESCRIPTION", data, "description", payload)
        seq_proj = etree.SubElement(project, "SUBMISSION_PROJECT")
        etree.SubElement(seq_proj, "SEQUENCING_PROJECT")
        root = etree.Element("PROJECT_SET")
        root.append(project)
        return self._to_string(root)

    def to_sample_xml(self, payload: CanopyEntityPayload) -> str:
        """Build SAMPLE_SET XML from a Canopy sample payload."""
        data = payload.data
        alias = self._alias("sample", payload.entity_id)
        sample = etree.Element("SAMPLE", alias=alias, center_name=self._center_name)
        self._required_sub(sample, "TITLE", data, "title", payload)
        name = etree.SubElement(sample, "SAMPLE_NAME")
        self._required_sub(name, "TAXON_ID", data, "tax_id", payload)
        self._required_sub(name, "SCIENTIFIC_NAME", data, "scientific_name", payload)
        common = data.get("common_name")
        if common:
            etree.SubElement(name, "COMMON_NAME").text = str(common)
        attrs = data.get("sample_attributes", [])
        if attrs:
            attrs_el = etree.SubElement(sample, "SAMPLE_ATTRIBUTES")
            for attr in attrs:
                attr_el = etree.SubElement(attrs_el, "SAMPLE_ATTRIBUTE")
                etree.SubElement(attr_el, "TAG").text = str(attr.get("tag", ""))
                etree.SubElement(attr_el, "VALUE").text = str(attr.get("value", ""))
                if "units" in attr:
                    etree.SubElement(attr_el, "UNITS").text = str(attr["units"])
        root = etree.Element("SAMPLE_SET")
        root.append(sample)
        return self._to_string(root)

    def to_experiment_xml(
        self,
        payload: CanopyEntityPayload,
        project_accession: str,
        sample_accession: str,
    ) -> str:
        """Build EXPERIMENT_SET XML, injecting resolved prerequisite accessions."""
        data = payload.data
        alias = self._alias("experiment", payload.entity_id)
        exp = etree.Element("EXPERIMENT", alias=alias, center_name=self._center_name)
        self._required_sub(exp, "TITLE", data, "title", payload)
        etree.SubElement(exp, "STUDY_REF", accession=project_accession)
        design = etree.SubElement(exp, "DESIGN")
        etree.SubElement(design, "DESIGN_DESCRIPTION")  # empty element — ENA requires it
        etree.SubElement(design, "SAMPLE_DESCRIPTOR", accession=sample_accession)
        lib = etree.SubElement(design, "LIBRARY_DESCRIPTOR")
        self._required_sub(lib, "LIBRARY_STRATEGY", data, "library_strategy", payload)
        self._required_sub(lib, "LIBRARY_SOURCE", data, "library_source", payload)
        self._required_sub(lib, "LIBRARY_SELECTION", data, "library_selection", payload)
        layout_el = etree.SubElement(lib, "LIBRARY_LAYOUT")
        layout_val = self._require_field(data, "library_layout", payload)
        etree.SubElement(layout_el, layout_val.upper())
        platform_val = self._require_field(data, "platform", payload)
        platform_el = etree.SubElement(exp, "PLATFORM")
        instrument_el = etree.SubElement(platform_el, platform_val.upper())
        self._required_sub(instrument_el, "INSTRUMENT_MODEL", data, "instrument_model", payload)
        root = etree.Element("EXPERIMENT_SET")
        root.append(exp)
        return self._to_string(root)

    def to_run_xml(
        self,
        payload: CanopyEntityPayload,
        experiment_accession: str,
    ) -> str:
        """Build RUN_SET XML, injecting resolved experiment accession.

        FTP upload is not yet implemented. This method produces the XML
        referencing files that are assumed to already be uploaded.
        See models/ena.py ENARunFile for the file metadata shape.
        """
        data = payload.data
        alias = self._alias("run", payload.entity_id)
        run = etree.Element("RUN", alias=alias, center_name=self._center_name)
        etree.SubElement(run, "EXPERIMENT_REF", accession=experiment_accession)
        files = data.get("files", [])
        if not files:
            raise TransformError(
                f"run '{payload.entity_id}': 'files' is required in payload data "
                f"but was missing or empty. Cannot build RUN XML."
            )
        data_block = etree.SubElement(run, "DATA_BLOCK")
        files_el = etree.SubElement(data_block, "FILES")
        for f in files:
            etree.SubElement(
                files_el,
                "FILE",
                filename=str(f.get("filename", "")),
                filetype=str(f.get("filetype", "")),
                checksum_method=str(f.get("checksum_method", "MD5")),
                checksum=str(f.get("checksum", "")),
            )
        root = etree.Element("RUN_SET")
        root.append(run)
        return self._to_string(root)

    # ------------------------------------------------------------------
    # Submission envelope
    # ------------------------------------------------------------------

    def build_submission_xml(
        self,
        action: str = "ADD",
        hold_until_date: str | None = None,
    ) -> str:
        """Build the SUBMISSION XML wrapper.

        The drop-box endpoint requires a SUBMISSION XML alongside the entity XML.
        If hold_until_date is provided (ISO 8601 date, e.g. '2026-01-01'),
        a HOLD action is added. HOLD is applicable to projects and samples only;
        callers are responsible for passing it only where appropriate.
        """
        submission = etree.Element("SUBMISSION")
        actions = etree.SubElement(submission, "ACTIONS")
        action_el = etree.SubElement(actions, "ACTION")
        etree.SubElement(action_el, action)
        if hold_until_date:
            hold_action = etree.SubElement(actions, "ACTION")
            etree.SubElement(hold_action, "HOLD", HoldUntilDate=hold_until_date)
        return self._to_string(submission)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _alias(entity_type: str, entity_id: str) -> str:
        """Generate an ENA alias from entity type + id.

        ENA uses the alias for idempotent resubmission — the same alias
        submitted twice returns the same accession rather than creating a duplicate.
        """
        return f"broker-{entity_type}-{entity_id}"

    @staticmethod
    def _to_string(element: etree._Element) -> str:
        return etree.tostring(element, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")

    @staticmethod
    def _require_field(data: dict, field: str, payload: CanopyEntityPayload) -> str:
        val = data.get(field)
        if not val:
            raise TransformError(
                f"{payload.entity_type} '{payload.entity_id}': required field "
                f"'{field}' is missing from payload data."
            )
        return str(val)

    def _required_sub(
        self,
        parent: etree._Element,
        tag: str,
        data: dict,
        field: str,
        payload: CanopyEntityPayload,
    ) -> etree._Element:
        val = self._require_field(data, field, payload)
        el = etree.SubElement(parent, tag)
        el.text = val
        return el
