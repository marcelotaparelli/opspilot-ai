"""Agent ports: the planning model and the external issue tracker."""

from dataclasses import dataclass
from typing import Literal, Protocol

from opspilot.domain import AppError


@dataclass(frozen=True)
class PlannerView:
    """Everything the model sees. Evidence is untrusted data, never instructions."""

    request: str
    projects: list[dict[str, object]]
    observations: list[dict[str, object]]
    steps_remaining: int


class Planner(Protocol):
    async def decide(self, view: PlannerView) -> object:
        """Return one raw decision object; the application validates it."""
        ...


@dataclass(frozen=True)
class Issue:
    id: int
    iid: int
    web_url: str
    description: str = ""


Outcome = Literal["created", "not_sent", "rejected", "unknown"]


@dataclass(frozen=True)
class CreateOutcome:
    """not_sent: the request provably never reached GitLab (safe to retry).
    rejected: GitLab answered and created nothing (retryable only for 429).
    unknown: the request may have been processed (timeout after send, 5xx, bad 2xx body).
    """

    kind: Outcome
    code: str
    issue: Issue | None = None
    retryable: bool = False


class TrackerUnavailable(AppError):
    code = "tracker_unavailable"
    status = 503


class IssueTracker(Protocol):
    async def create_issue(
        self,
        project_id: int,
        title: str,
        description: str,
        labels: tuple[str, ...],
        assignee_ids: tuple[int, ...],
    ) -> CreateOutcome: ...

    async def find_by_marker(self, project_id: int, marker: str) -> list[Issue]:
        """Issues whose description contains the marker; raises TrackerUnavailable."""
        ...

    async def close(self) -> None: ...


class AgentError(AppError):
    """Controlled agent API error with a fixed, loggable code."""

    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
