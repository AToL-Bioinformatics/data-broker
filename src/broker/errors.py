"""Custom exception hierarchy for the broker.

All broker-specific errors inherit from BrokerError so callers can
catch the base class or a specific subclass as needed.
"""


class BrokerError(Exception):
    """Base for all broker errors."""


class PrerequisiteMissingError(BrokerError):
    """A required accession prerequisite could not be resolved.

    Raised when:
    - CLI args do not supply the value, AND
    - the Canopy payload does not include the value, AND
    - (in bulk mode) no succeeded entity in the same attempt provides it.

    Never silently auto-submits dependencies; always fails explicitly.
    """

    def __init__(self, entity_type: str, entity_id: str, missing: list[str]) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.missing = missing
        fields = ", ".join(missing)
        super().__init__(
            f"{entity_type} '{entity_id}' cannot be submitted: "
            f"missing prerequisite accession(s): {fields}. "
            f"Submit the required prerequisite entit{'y' if len(missing) == 1 else 'ies'} first, "
            f"or supply the accession(s) via CLI flags."
        )


class ENASubmissionError(BrokerError):
    """ENA returned a non-success HTTP response.

    Note: ENAClient itself does not raise this — it returns an
    ENASubmissionResult with success=False. This error is raised
    only when a caller explicitly needs to surface the failure.
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"ENA returned HTTP {status_code}: {body[:300]}")


class CanopyError(BrokerError):
    """Canopy API returned an unexpected or error response."""

    def __init__(self, status_code: int, body: str, url: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.url = url
        super().__init__(
            f"Canopy API error {status_code}"
            + (f" ({url})" if url else "")
            + f": {body[:300]}"
        )


class StateStoreError(BrokerError):
    """Failure reading or writing persisted state."""


class AttemptNotFoundError(BrokerError):
    """Attempt ID not found in state store."""

    def __init__(self, attempt_id: str) -> None:
        self.attempt_id = attempt_id
        super().__init__(f"No saved attempt found with ID: {attempt_id}")


class ConfigurationError(BrokerError):
    """Required configuration (env vars, credentials) is missing or invalid."""


class ValidationError(BrokerError):
    """Entity failed pre-submission validation."""

    def __init__(self, entity_type: str, entity_id: str, errors: list[str]) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.errors = errors
        super().__init__(
            f"{entity_type} '{entity_id}' failed validation: {'; '.join(errors)}"
        )
