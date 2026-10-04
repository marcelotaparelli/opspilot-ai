"""Strict, bounded HTTP contracts, with metadata normalization."""

import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opspilot.chunking import normalize_content

if TYPE_CHECKING:
    from opspilot.agent.store import RunBundle

BoundedTag = Annotated[str, Field(min_length=1, max_length=40)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


def clean(value: str) -> str:
    result = unicodedata.normalize("NFC", value).strip()
    if any(unicodedata.category(char).startswith("C") for char in result):
        raise ValueError("control characters are not allowed")
    return result


class MetadataInput(Contract):
    title: str = Field(min_length=1, max_length=200)
    source: str | None = Field(default=None, min_length=1, max_length=500)
    tags: list[BoundedTag] = Field(default_factory=list, max_length=20)

    @field_validator("title", "source", mode="before")
    @classmethod
    def normalized_text(cls, value: object) -> object:
        return clean(value) if isinstance(value, str) else value

    @field_validator("tags", mode="before")
    @classmethod
    def normalized_tags(cls, value: object) -> object:
        if isinstance(value, list):
            # Bound before deduplication so oversized arrays cannot bypass validation.
            if len(value) > 20:
                raise ValueError("too many tags")
            if all(isinstance(tag, str) for tag in value):
                return sorted({clean(tag).casefold() for tag in value})
        return value


class DocumentInput(Contract):
    content: str = Field(min_length=1, max_length=100_000)
    metadata: MetadataInput

    @model_validator(mode="after")
    def normalized_document(self) -> Self:
        self.content = normalize_content(self.content)
        return self


class DocumentOutput(Contract):
    document_id: UUID
    chunk_ids: list[UUID]
    request_id: str


class QueryInput(Contract):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("question", mode="before")
    @classmethod
    def normalized_question(cls, value: object) -> object:
        return clean(value) if isinstance(value, str) else value


class Citation(Contract):
    chunk_id: UUID
    document_id: UUID
    ordinal: int
    title: str
    source: str | None
    start_offset: int
    end_offset: int
    quote: str


class RetrievedChunk(Citation):
    score: float


class QueryOutput(Contract):
    answer: str
    citations: list[Citation]
    retrieved_chunks: list[RetrievedChunk]
    request_id: str


class ErrorOutput(Contract):
    error: str
    request_id: str


class AgentRunInput(Contract):
    request: str = Field(min_length=1, max_length=2000)

    @field_validator("request", mode="before")
    @classmethod
    def normalized_request(cls, value: object) -> object:
        return clean(value) if isinstance(value, str) else value


class DecisionInput(Contract):
    # The approver must name the exact action they reviewed.
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ContextRef(Contract):
    chunk_id: UUID
    document_id: UUID
    title: str


class ProposalView(Contract):
    action_hash: str
    project: str
    project_id: int
    title: str
    description: str
    labels: list[str]
    assignee_ids: list[int]
    created_at: datetime


class ApprovalView(Contract):
    decision: str
    action_hash: str
    decided_by: str
    decided_at: datetime


class ExecutionView(Contract):
    status: str
    attempts: int
    issue_iid: int | None
    issue_url: str | None
    last_error: str | None


class AgentRunView(Contract):
    run_id: UUID
    status: str
    requested_by: str
    steps: int
    answer: str | None
    cited_chunk_ids: list[UUID]
    context: list[ContextRef]
    proposal: ProposalView | None
    approval: ApprovalView | None
    execution: ExecutionView | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    request_id: str


def run_view(bundle: "RunBundle", request_id: str) -> AgentRunView:
    run, proposal, approval, execution = (
        bundle.run,
        bundle.proposal,
        bundle.approval,
        bundle.execution,
    )
    state = run.state
    return AgentRunView(
        run_id=run.id,
        status=run.status,
        requested_by=run.requested_by,
        steps=run.steps,
        answer=state.get("answer"),
        cited_chunk_ids=[UUID(item) for item in state.get("cited_chunk_ids", [])],
        context=[
            ContextRef(
                chunk_id=UUID(ref["chunk_id"]),
                document_id=UUID(ref["document_id"]),
                title=ref["title"],
            )
            for ref in state.get("context", [])
        ],
        proposal=ProposalView(
            action_hash=proposal.action_hash,
            project=proposal.action.project,
            project_id=proposal.action.project_id,
            title=proposal.action.title,
            description=proposal.action.description,
            labels=list(proposal.action.labels),
            assignee_ids=list(proposal.action.assignee_ids),
            created_at=proposal.created_at,
        )
        if proposal
        else None,
        approval=ApprovalView(
            decision=approval.decision,
            action_hash=approval.action_hash,
            decided_by=approval.decided_by,
            decided_at=approval.decided_at,
        )
        if approval
        else None,
        execution=ExecutionView(
            status=execution.status,
            attempts=execution.attempts,
            issue_iid=execution.issue_iid,
            issue_url=execution.issue_url,
            last_error=execution.last_error,
        )
        if execution
        else None,
        error=state.get("error"),
        created_at=run.created_at,
        updated_at=run.updated_at,
        request_id=request_id,
    )
