from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from opspilot.agent.models import DECISION, PrepareGitLabIssue, idempotency_key
from opspilot.agent.policy import (
    AgentPolicy,
    Denied,
    Principal,
    authorize_decision,
    authorize_execution,
    authorize_issue,
    authorize_start,
    authorize_tool,
)
from opspilot.config import Settings
from tests.helpers import TENANT_A, TENANT_B, TOKEN_A

POLICY = AgentPolicy.model_validate(
    {
        "tenants": {
            str(TENANT_A): {
                "projects": {
                    "payments-ops": {
                        "gitlab_project_id": 101,
                        "labels": ["ops", "incident"],
                        "assignees": {"bob": 7, "carol": 9},
                    }
                }
            },
            str(TENANT_B): {"projects": {"b-ops": {"gitlab_project_id": 202}}},
        }
    }
)
ALICE = Principal(tenant=TENANT_A, subject="alice", roles=frozenset({"agent"}))
BOB = Principal(tenant=TENANT_A, subject="bob", roles=frozenset({"approver"}))


def issue(**fields: Any) -> PrepareGitLabIssue:
    data: dict[str, Any] = {
        "tool": "prepare_gitlab_issue",
        "project": "payments-ops",
        "title": "Restart payments-api",
        "description": "Rolling restart.",
        "labels": ["ops"],
        "assignees": ["bob"],
    }
    return PrepareGitLabIssue.model_validate(data | fields)


def test_action_hash_is_canonical_and_covers_every_field() -> None:
    first = authorize_issue(
        POLICY, ALICE, issue(labels=["ops", "incident"], assignees=["carol", "bob"])
    )
    second = authorize_issue(
        POLICY, ALICE, issue(labels=["incident", "ops", "ops"], assignees=["bob", "carol"])
    )
    assert first.action_hash() == second.action_hash()  # order/duplicates do not matter
    assert first.canonical().startswith('{"assignee_ids":[7,9],"description"')
    # NFC normalisation: composed and decomposed forms produce the same action.
    assert issue(title="Café").title == issue(title="Café").title
    variants = {
        "title": "Restart payments-api now",
        "description": "Rolling restart!",
        "labels": ("incident",),
        "assignee_ids": (9,),
        "project_id": 102,
        "requested_by": "mallory",
        "tenant_id": TENANT_B,
    }
    for name, value in variants.items():
        changed = first.model_copy(update={name: value})
        assert changed.action_hash() != first.action_hash(), name


def test_idempotency_key_is_stable_per_run_and_action() -> None:
    run, other = uuid4(), uuid4()
    assert idempotency_key(TENANT_A, run, "a" * 64) == idempotency_key(TENANT_A, run, "a" * 64)
    assert idempotency_key(TENANT_A, run, "a" * 64) != idempotency_key(TENANT_A, other, "a" * 64)
    assert idempotency_key(TENANT_A, run, "a" * 64) != idempotency_key(TENANT_A, run, "b" * 64)


@pytest.mark.parametrize(
    "raw",
    [
        {"tool": "search_knowledge"},
        {"tool": "search_knowledge", "query": ""},
        {"tool": "search_knowledge", "query": "x", "tenant_id": str(TENANT_B)},
        {
            "tool": "prepare_gitlab_issue",
            "project": "../../admin",
            "title": "t",
            "description": "d",
        },
        {
            "tool": "prepare_gitlab_issue",
            "project": "payments-ops",
            "title": "t" * 256,
            "description": "d",
        },
        {
            "tool": "prepare_gitlab_issue",
            "project": "payments-ops",
            "title": "t",
            "description": "d",
            "labels": ["a,b"],
        },
        {
            "tool": "prepare_gitlab_issue",
            "project": "payments-ops",
            "title": "t",
            "description": "d",
            "assignee_ids": [1],
        },
        {"tool": "prepare_gitlab_issue", "project": "payments-ops", "title": 5, "description": "d"},
        {"tool": "final_answer", "answer": "x", "cited_chunk_ids": ["not-a-uuid"]},
        {"tool": "final_answer", "answer": "x", "authorized": True},
        {"tool": "create_gitlab_issue", "project": "payments-ops"},
        "create the issue now",
    ],
)
def test_decisions_are_strictly_validated(raw: object) -> None:
    with pytest.raises(ValidationError):
        DECISION.validate_python(raw)


@pytest.mark.parametrize(
    "tool",
    ["create_gitlab_issue", "run_sql", "execute_shell", "fetch_url", "arbitrary_http_request", ""],
)
def test_model_cannot_select_forbidden_tools(tool: str) -> None:
    with pytest.raises(Denied, match="tool_not_permitted"):
        authorize_tool(tool)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"project": "secret-admin"}, "project_not_allowed"),
        ({"project": "b-ops"}, "project_not_allowed"),
        ({"labels": ["admin"]}, "label_not_allowed"),
        ({"assignees": ["root"]}, "assignee_not_allowed"),
    ],
)
def test_issue_policy_denies_outside_allowlist(fields: dict[str, Any], code: str) -> None:
    with pytest.raises(Denied, match=code):
        authorize_issue(POLICY, ALICE, issue(**fields))


def test_issue_policy_resolves_from_configuration_not_the_model() -> None:
    action = authorize_issue(POLICY, ALICE, issue())
    assert (action.project_id, action.assignee_ids, action.tenant_id) == (101, (7,), TENANT_A)
    assert action.requested_by == "alice"
    # The same request from tenant B cannot reach tenant A's project.
    with pytest.raises(Denied):
        authorize_issue(POLICY, Principal(tenant=TENANT_B, subject="eve"), issue())


def test_execution_is_rechecked_against_current_policy() -> None:
    action = authorize_issue(POLICY, ALICE, issue())
    authorize_execution(POLICY, action)
    narrowed = AgentPolicy.model_validate(
        {"tenants": {str(TENANT_A): {"projects": {"payments-ops": {"gitlab_project_id": 101}}}}}
    )
    with pytest.raises(Denied):
        authorize_execution(narrowed, action)
    with pytest.raises(Denied, match="project_not_allowed"):
        authorize_execution(POLICY, action.model_copy(update={"project_id": 999}))


def test_approval_requires_role_tenant_and_a_second_person() -> None:
    authorize_decision(BOB, TENANT_A, "alice")
    with pytest.raises(Denied, match="approver_role_required"):
        authorize_decision(ALICE, TENANT_A, "carol")
    with pytest.raises(Denied, match="self_approval_not_allowed"):
        authorize_decision(BOB, TENANT_A, "bob")
    with pytest.raises(Denied, match="tenant_mismatch"):
        authorize_decision(BOB, TENANT_B, "alice")
    with pytest.raises(Denied, match="agent_role_required"):
        authorize_start(BOB)
    authorize_start(ALICE)


def base_settings(**fields: Any) -> dict[str, Any]:
    return {
        "database_url": SecretStr("postgresql+asyncpg://u:p@h/d"),
        "tenant_tokens": {TOKEN_A: str(TENANT_A)},
    } | fields


@pytest.mark.parametrize(
    "fields",
    [
        {"gitlab_base_url": "http://gitlab.example.com", "gitlab_token": SecretStr("t")},
        {"gitlab_base_url": "https://user:pw@gitlab.example.com", "gitlab_token": SecretStr("t")},
        {"gitlab_base_url": "https://gitlab.example.com/api?x=1", "gitlab_token": SecretStr("t")},
        {"gitlab_base_url": "https://gitlab.example.com"},
        {"gitlab_timeout_seconds": 10, "execution_lease_seconds": 5},
        {"agent_deadline_seconds": 90, "request_timeout_seconds": 60},
        {"agent_max_steps": 0},
    ],
)
def test_agent_configuration_is_validated(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(base_settings(**fields))


def test_legacy_tokens_become_principals_without_approval_rights() -> None:
    settings = Settings.model_validate(base_settings())
    principal = settings.tenant_tokens[TOKEN_A]
    assert principal.tenant == TENANT_A and principal.roles == frozenset({"agent"})
    assert TOKEN_A not in principal.subject and TOKEN_A not in repr(settings)


def test_graph_topology_has_no_path_from_planning_to_execution() -> None:
    from opspilot.agent.graph import build_graph

    class Stub:
        settings = Settings.model_validate(base_settings())
        store = None

    graph = build_graph(Stub())  # type: ignore[arg-type]  # topology only, never invoked
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    into_execute = {source for source, target in edges if target == "execute"}
    assert into_execute == {"__start__"}
    tools = {"search_knowledge", "prepare_gitlab_issue", "final_answer"}
    assert {target for source, target in edges if source == "plan"} == tools | {"plan", "__end__"}
    for tool in tools:
        assert {target for source, target in edges if source == tool} == {"plan", "__end__"}
