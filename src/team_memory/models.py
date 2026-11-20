from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=250)
]
Space = Annotated[
    str, StringConstraints(pattern=r"^(project|group|personal):[\w.-]{1,120}$")
]
Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)
]
Role = Literal["read", "write", "review"]
# Sources written by older versions use this key for revision identity.
WORKSPACE_STORAGE_KEY = "copaw_memory"


def public_adapter(value):
    return "assistant_workspace" if value == WORKSPACE_STORAGE_KEY else value


Adapter = Annotated[
    Literal[
        "project_document",
        "personal_note",
        "codex_summary",
        "feishu_group",
        "assistant_workspace",
        "agent_note",
    ],
    BeforeValidator(public_adapter),
]


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(Input):
    space: Space
    adapter: Adapter
    external_id: Name
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500_000)
    ]
    revision: Name | None = None
    occurred_at: AwareDatetime
    author: Name | None = None


class Evidence(Input):
    source_id: Name
    quote: Text


class Record(Input):
    space: Space
    claim: Text
    request_id: Name
    kind: Literal[
        "decision",
        "preference",
        "assignment",
        "risk",
        "progress",
        "question",
        "procedure",
        "observation",
    ] = "observation"
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    subjects: list[Name] = Field(default_factory=list, max_length=30)
    topic: Name | None = None
    memory_id: Name | None = None
    base_version: int | None = Field(None, ge=1)
    effective_at: AwareDatetime | None = None
    recheck_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_base(self):
        if (self.memory_id is None) != (self.base_version is None):
            raise ValueError("memory_id and base_version are required together")
        return self


class Decision(Input):
    action: Literal["confirm", "edit", "reject", "defer"]
    expected_version: int = Field(ge=1)
    claim: Text | None = None
    remind_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "edit" and not self.claim:
            raise ValueError("edited claim required")
        if self.action == "defer" and (not self.remind_at or self.remind_at <= now()):
            raise ValueError("future reminder required")
        return self


class Query(Input):
    space: Space
    query: str = Field(min_length=1, max_length=600)
    limit: int = Field(10, ge=1, le=50)


class Context(Input):
    space: Space
    budget: int = Field(6000, ge=200, le=30000)
    include_pending: bool = False


class Failure(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)
