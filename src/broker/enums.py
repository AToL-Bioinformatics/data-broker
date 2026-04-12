from enum import StrEnum


class EntityType(StrEnum):
    PROJECT = "project"
    SAMPLE = "sample"
    EXPERIMENT = "experiment"
    RUN = "run"


# Canonical dependency order for bulk submissions.
ENTITY_DEPENDENCY_ORDER: list[EntityType] = [
    EntityType.PROJECT,
    EntityType.SAMPLE,
    EntityType.EXPERIMENT,
    EntityType.RUN,
]


class AttemptMode(StrEnum):
    BULK = "bulk"
    TARGETED = "targeted"


class AttemptStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"  # some entities succeeded, some failed


class EntitySubmissionStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"  # POST sent, response not yet confirmed
    SKIPPED = "skipped"  # dry-run
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SubmissionMode(StrEnum):
    NORMAL = "normal"
    DRY_RUN = "dry_run"
    VALIDATE_ONLY = "validate_only"
