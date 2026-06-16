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

# ---------------------------------------------------------------------------
# Required ATOL sample attributes
# ---------------------------------------------------------------------------
# All of these must appear in every ENA sample submission.
# - "project name" is always forced to the canonical ATOL value regardless of
#   what Canopy sends.
# - All other tags default to "missing:not provided" when absent from the
#   Canopy payload, so that the submission is valid even when data is
#   incomplete at the time of submission.
# Required experiment library fields — substituted when absent from the payload.
# library_layout is special: it becomes an XML element name (<PAIRED/> or <SINGLE/>),
# so "missing:not provided" would be an invalid tag — default to PAIRED instead.
_REQUIRED_EXPERIMENT_FIELDS: dict[str, str] = {
    "library_name": "missing:not provided",
    "library_strategy": "missing:not provided",
    "library_source": "missing:not provided",
    "library_selection": "missing:not provided",
    "library_layout": "PAIRED",
    "library_construction_protocol": "missing:not provided",
}

_REQUIRED_SAMPLE_ATTRIBUTES: dict[str, str] = {
    "project name": "atol_genome_engine",
    "lifestage": "missing:not provided",
    "organism part": "missing:not provided",
    "collected_by": "missing:not provided",
    # TODO handle missing "collection date" - temp fix for testing
    "collection date": "2026-01-01",
    "geographic location (region and locality)": "missing:not provided",
    "habitat": "missing:not provided",
    "sex": "missing:not provided",
    # TODO fix below, temp fix setting to Autrtalia for now to unblock ATOL testing — we need to update the test data and then remove this default
    "collection method": "missing:not provided",
    "geographic location (country and/or sea)": "missing:not provided",
    "collecting institution": "missing:not provided",
}


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
        sample = self._build_sample_element(payload)
        root = etree.Element("SAMPLE_SET")
        root.append(sample)
        return self._to_string(root)

    def to_sample_modify_xml(
        self,
        payload: CanopyEntityPayload,
        ena_accession: str,
        tolid: str,
    ) -> str:
        """Build a SAMPLE_SET XML for a MODIFY submission.

        Identical to the original sample XML except:
        - The ``accession`` attribute is set on ``<SAMPLE>`` so ENA can locate
          the existing record.
        - A ``tolid`` SAMPLE_ATTRIBUTE is injected before the full attribute
          list is built (so it goes through ``_apply_required_sample_attributes``
          alongside any other user-supplied attributes).
        """
        # Inject tolid into a copy of sample_attributes before building
        existing_attrs = list(payload.data.get("sample_attributes", []))
        attrs_with_tolid = [{"tag": "tolid", "value": tolid}] + existing_attrs

        # Build a patched payload dict so the shared builder picks up the tolid
        patched_data = {**payload.data, "sample_attributes": attrs_with_tolid}
        patched_payload = payload.model_copy(update={"data": patched_data})

        sample = self._build_sample_element(patched_payload)
        sample.set("accession", ena_accession)
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
        etree.SubElement(lib, "LIBRARY_NAME").text = self._field_or_default(data, "library_name")
        etree.SubElement(lib, "LIBRARY_STRATEGY").text = self._field_or_default(data, "library_strategy")
        etree.SubElement(lib, "LIBRARY_SOURCE").text = self._field_or_default(data, "library_source")
        etree.SubElement(lib, "LIBRARY_SELECTION").text = self._field_or_default(data, "library_selection")
        layout_el = etree.SubElement(lib, "LIBRARY_LAYOUT")
        layout_val = self._field_or_default(data, "library_layout")
        etree.SubElement(layout_el, layout_val.upper())
        etree.SubElement(lib, "LIBRARY_CONSTRUCTION_PROTOCOL").text = self._field_or_default(data, "library_construction_protocol")
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
        files_data = data.get("files", [])
        if not files_data:
            raise TransformError(
                f"run '{payload.entity_id}': 'files' list is missing or empty"
            )
        alias = self._alias("run", payload.entity_id)
        run = etree.Element("RUN", alias=alias, center_name=self._center_name)
        etree.SubElement(run, "EXPERIMENT_REF", accession=experiment_accession)
        data_block = etree.SubElement(run, "DATA_BLOCK")
        files_el = etree.SubElement(data_block, "FILES")
        for f in files_data:
            etree.SubElement(
                files_el,
                "FILE",
                filename=f.get("filename", "missing:not provided"),
                filetype=f.get("filetype", "missing:not provided"),
                checksum_method=f.get("checksum_method", "MD5"),
                checksum=f.get("checksum", "missing:not provided"),
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

    def _build_sample_element(self, payload: CanopyEntityPayload) -> etree._Element:
        """Build a ``<SAMPLE>`` element from a payload.

        Used by both ``to_sample_xml`` (ADD) and ``to_sample_modify_xml``
        (MODIFY).  The caller is responsible for setting the ``accession``
        attribute on the returned element when needed.
        """
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
        attrs = self._apply_required_sample_attributes(data.get("sample_attributes", []))
        if attrs:
            attrs_el = etree.SubElement(sample, "SAMPLE_ATTRIBUTES")
            for attr in attrs:
                attr_el = etree.SubElement(attrs_el, "SAMPLE_ATTRIBUTE")
                etree.SubElement(attr_el, "TAG").text = str(attr.get("tag", ""))
                etree.SubElement(attr_el, "VALUE").text = str(attr.get("value", ""))
                if "units" in attr:
                    etree.SubElement(attr_el, "UNITS").text = str(attr["units"])
        return sample

    @staticmethod
    def _field_or_default(data: dict, field: str) -> str:
        """Return the field value from data, or the configured default.

        Falls back to ``_REQUIRED_EXPERIMENT_FIELDS`` for experiment library
        fields.  Returns an empty string if the field is not in the defaults
        map (callers that need a hard failure should use ``_require_field``).
        """
        val = data.get(field)
        if val:
            return str(val)
        return _REQUIRED_EXPERIMENT_FIELDS.get(field, "")

    @staticmethod
    def _apply_required_sample_attributes(attrs: list[dict]) -> list[dict]:
        """Ensure all required ATOL sample attributes are present.

        Rules:
        - ``project name`` is always forced to ``"atol_genome_engine"``
          regardless of what the Canopy payload contains.
        - All other required tags are injected with ``"missing:not provided"``
          only when absent — existing values are left untouched.
        - Tag matching is case-insensitive so "Lifestage" and "lifestage" are
          treated as the same tag.
        """
        # Build a normalised {lower_tag: list_index} map for O(1) lookup
        tag_index: dict[str, int] = {
            a.get("tag", "").lower(): i for i, a in enumerate(attrs)
        }
        result = list(attrs)

        for tag, default_value in _REQUIRED_SAMPLE_ATTRIBUTES.items():
            idx = tag_index.get(tag.lower())
            if tag == "project name":
                # Always enforce the canonical ATOL value
                if idx is not None:
                    result[idx] = {**result[idx], "value": default_value}
                else:
                    result.append({"tag": tag, "value": default_value})
            else:
                # Inject default only when the tag is absent
                if idx is None:
                    result.append({"tag": tag, "value": default_value})

        return result

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
        if tag == "TITLE" and payload.entity_type == "project":
            val = self._with_taxon_id_suffix(val, payload)
        el = etree.SubElement(parent, tag)
        el.text = val
        return el

    @staticmethod
    def _with_taxon_id_suffix(value: str, payload: CanopyEntityPayload) -> str:
        tax_id = payload.data.get("tax_id") or payload.data.get("taxon_id")
        if not tax_id:
            return value
        suffix = f" ({tax_id})"
        if value.endswith(suffix):
            return value
        return f"{value}{suffix}"
