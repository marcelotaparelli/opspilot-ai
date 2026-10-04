"""Agent values: strict model-decision schemas, the canonical issue action and run state."""

import hashlib
import json
import unicodedata
from typing import Annotated, Literal, TypedDict
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

RunStatus = Literal[
    "planning",
    "awaiting_approval",
    "approved",
    "executing",
    "ambiguous",
    "succeeded",
    "answered",
    "rejected",
    "failed",
]
ExecutionStatus = Literal["pending", "executing", "succeeded", "ambiguous", "failed_terminal"]
TERMINAL: frozenset[str] = frozenset({"succeeded", "answered", "rejected", "failed"})
LABEL = r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,49}$"


def _clean(value: object) -> object:
    if isinstance(value, str):
        text = unicodedata.normalize("NFC", value).strip()
        if "\x00" in text:
            raise ValueError("NUL not allowed")
        return text
    return value


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchKnowledge(Decision):
    tool: Literal["search_knowledge"]
    query: str = Field(min_length=1, max_length=500)

    @field_validator("query", mode="before")
    @classmethod
    def normalised(cls, value: object) -> object:
        return _clean(value)


class PrepareGitLabIssue(Decision):
    tool: Literal["prepare_gitlab_issue"]
    project: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=8000)
    labels: list[Annotated[str, Field(pattern=LABEL)]] = Field(default_factory=list, max_length=10)
    assignees: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")]] = Field(
        default_factory=list, max_length=5
    )

    @field_validator("title", "description", mode="before")
    @classmethod
    def normalised(cls, value: object) -> object:
        return _clean(value)


class FinalAnswer(Decision):
    tool: Literal["final_answer"]
    answer: str = Field(min_length=1, max_length=4000)
    cited_chunk_ids: list[UUID] = Field(default_factory=list, max_length=10)


AnyDecision = Annotated[
    SearchKnowledge | PrepareGitLabIssue | FinalAnswer, Field(discriminator="tool")
]
DECISION: TypeAdapter[SearchKnowledge | PrepareGitLabIssue | FinalAnswer] = TypeAdapter(AnyDecision)


class IssueAction(BaseModel):
    """Exactly what will be sent to GitLab, after authorization; immutable once stored."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tool: Literal["create_gitlab_issue"] = "create_gitlab_issue"
    tenant_id: UUID
    requested_by: str
    project: str
    project_id: int
    title: str
    description: str
    labels: tuple[str, ...]
    assignee_ids: tuple[int, ...]

    def canonical(self) -> str:
        """Sorted keys, no whitespace, UTF-8; only strings, ints and lists occur."""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def action_hash(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()


def idempotency_key(tenant: UUID, run_id: UUID, action_hash: str) -> str:
    """Stable per approved action: same run + same action always yields the same key."""
    return f"opspilot-{uuid5(NAMESPACE_URL, f'opspilot:action:{tenant}:{run_id}:{action_hash}')}"


def marker_footer(key: str) -> str:
    # HTML comment: invisible in rendered Markdown, searchable in the description.
    return f"\n\n<!-- opspilot-action: {key} -->"


class AgentState(TypedDict):
    """LangGraph state. Tenant and subject are set by the application, never by the model."""

    run_id: str
    tenant_id: str
    subject: str
    request: str
    status: RunStatus
    steps: int
    invalid_outputs: int
    observations: list[dict[str, object]]
    context: list[dict[str, str]]
    decision: dict[str, object] | None
    answer: str | None
    cited_chunk_ids: list[str]
    error: str | None
