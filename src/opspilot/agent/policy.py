"""Deterministic authorization for agent actions. The model never supplies authority.

Tenant and subject come from the authenticated principal; projects, labels and assignees
come from configuration. A model may only *name* an allowed project alias, label or
assignee; everything is resolved and checked here.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from opspilot.agent.models import (
    IssueAction,
    PrepareGitLabIssue,
)
from opspilot.domain import AppError

Role = Literal["agent", "approver"]
ALIAS = r"^[a-z0-9][a-z0-9-]{0,63}$"
# Tools the model may request. create_gitlab_issue is executed only by the application
# after approval; it is deliberately absent here.
MODEL_TOOLS = frozenset({"search_knowledge", "prepare_gitlab_issue", "final_answer"})


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant: UUID
    subject: str = Field(pattern=r"^[A-Za-z0-9._@-]{1,64}$")
    roles: frozenset[Role] = frozenset()


class ProjectPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    gitlab_project_id: int = Field(ge=1)
    labels: frozenset[str] = frozenset()
    # Assignable usernames mapped to GitLab user IDs; the model never supplies raw IDs.
    assignees: dict[str, int] = Field(default_factory=dict)


class TenantPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    projects: dict[str, ProjectPolicy] = Field(default_factory=dict)


class AgentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenants: dict[UUID, TenantPolicy] = Field(default_factory=dict)

    def projects(self, tenant: UUID) -> dict[str, ProjectPolicy]:
        policy = self.tenants.get(tenant)
        return dict(policy.projects) if policy else {}


class Denied(AppError):
    """Authorization refusal with a fixed, loggable reason code."""

    status = 403

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def authorize_tool(tool: str) -> None:
    if tool not in MODEL_TOOLS:
        raise Denied("tool_not_permitted")


def authorize_start(principal: Principal) -> None:
    if "agent" not in principal.roles:
        raise Denied("agent_role_required")


def authorize_issue(
    policy: AgentPolicy, principal: Principal, request: PrepareGitLabIssue
) -> IssueAction:
    """Resolve and check a model-proposed issue; returns the canonical action or raises."""
    project = policy.projects(principal.tenant).get(request.project)
    if project is None:
        raise Denied("project_not_allowed")
    labels = sorted(set(request.labels))
    if not set(labels) <= project.labels:
        raise Denied("label_not_allowed")
    if any(name not in project.assignees for name in request.assignees):
        raise Denied("assignee_not_allowed")
    return IssueAction(
        tenant_id=principal.tenant,
        requested_by=principal.subject,
        project=request.project,
        project_id=project.gitlab_project_id,
        title=request.title,
        description=request.description,
        labels=tuple(labels),
        assignee_ids=tuple(sorted({project.assignees[name] for name in request.assignees})),
    )


def authorize_execution(policy: AgentPolicy, action: IssueAction) -> None:
    """Re-check a stored action against the *current* policy just before the side effect."""
    project = policy.projects(action.tenant_id).get(action.project)
    if project is None or project.gitlab_project_id != action.project_id:
        raise Denied("project_not_allowed")
    if not set(action.labels) <= project.labels:
        raise Denied("label_not_allowed")
    if not set(action.assignee_ids) <= set(project.assignees.values()):
        raise Denied("assignee_not_allowed")


def authorize_decision(principal: Principal, tenant: UUID, requested_by: str) -> None:
    """Approver must hold the role, belong to the run's tenant and not be the requester."""
    if "approver" not in principal.roles:
        raise Denied("approver_role_required")
    if principal.tenant != tenant:
        raise Denied("tenant_mismatch")
    if principal.subject == requested_by:
        raise Denied("self_approval_not_allowed")
