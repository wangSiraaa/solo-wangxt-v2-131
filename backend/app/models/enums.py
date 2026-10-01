import enum


class ManifestStatus(str, enum.Enum):
    created = "created"
    completed = "completed"
    completed_with_defects = "completed_with_defects"
    validation_failed = "validation_failed"


class BlockStatus(str, enum.Enum):
    pending = "pending"
    stored = "stored"


class CalibrationStatus(str, enum.Enum):
    draft = "draft"
    active = "active"
    superseded = "superseded"


class TaskState(str, enum.Enum):
    pending = "pending"
    running = "running"
    cancel_requested = "cancel_requested"
    canceled = "canceled"
    failed = "failed"
    published = "published"


class ReviewState(str, enum.Enum):
    current = "current"
    review_required = "review_required"


class QualitySeverity(str, enum.Enum):
    info = "info"
    warning = "warning"
    fatal = "fatal"


class ReportStatus(str, enum.Enum):
    draft = "draft"
    complete = "complete"
    incomplete = "incomplete"
    canceled = "canceled"
    review_required = "review_required"
