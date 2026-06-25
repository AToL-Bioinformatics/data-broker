# Broker Runbook

## Before Running Anything

Read these sections in [README.md](../README.md):

- `Configuration`
- `Commands`
- `Prerequisite accessions`
- `Hold dates`
- `State and receipts`

This runbook only covers the operational points that are easy to miss.

## Operational Cautions

- Do not mix ENA dev submissions with prod/AWS Canopy reporting.
- `run` submission assumes the raw read files are already transferred to ENA.
- The broker does not perform the raw file transfer step.
- Raw file transfer must happen before `run` submissions are attempted.
- The genome launcher handles raw data transfer to ENA and assembly submission separately.
- `--validate-only` should not be relied on as a true no-submit command yet.

## Success Checks

### Local checks

- Use the attempt state and receipt locations documented in [README.md](../README.md).
- Confirm the CLI ended with `COMPLETED` or the expected `PARTIAL`.
- Inspect the saved state file.
- Inspect the raw ENA receipt for the entity.

### ENA Reports API

- [ENA Reports API](https://www.ebi.ac.uk/ena/submit/report/swagger-ui/index.html#/)

Useful for API-based verification of submission/reporting state for private/released data.

### Webin portal

- [ENA Webin login](https://www.ebi.ac.uk/ena/submit/webin/login)

Useful to verify submission of, and manually update samples, projects, experiments and runs we have submitted.

### Webin browser / Broswer API / Portal API

These can be used to explore and view public/released data only. Documentation can be found on ENA website.

## Failure Handling

Missing prerequisite accession:

- Check the prerequisite rules and examples in [README.md](../README.md).
- rerun with `--project-accession`, `--sample-accession`, or `--experiment-accession`
- or fix the upstream Canopy payload/prerequisites

Interrupted run:

- Use the attempt state file location documented in [README.md](../README.md).
- use the attempt ID from the prior run
- rerun `broker resume --attempt-id <attempt-id>`

ENA rejection:

- inspect the raw receipt first
- inspect the XML-building assumptions for that entity type
- correct the payload upstream or rerun with explicit prerequisites if needed

Canopy did not get the result:

- local state and receipt are the source of truth for what the broker actually did
- check whether reporting/finalisation failed after ENA submission

## Some relevant resources

- [ENA documentation](https://ena-docs.readthedocs.io/en/latest/)
- [ENA Webin login](https://www.ebi.ac.uk/ena/submit/webin/login)
- [ENA Reports API](https://www.ebi.ac.uk/ena/submit/report/swagger-ui/index.html#/)
Filter files
- [Browser API record retrieval](https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/browser-api.html)
- [ENA file reports](https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access/file-reports.html)