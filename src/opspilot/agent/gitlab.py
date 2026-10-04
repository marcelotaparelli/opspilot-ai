"""GitLab REST v4 adapter. Base URL and token come only from validated configuration.

The token is sent in PRIVATE-TOKEN and never logged. Redirects are not followed, so the
token cannot be forwarded to another host. Project IDs are integers from the policy, so no
model-provided text reaches a URL path. Every outcome is classified by whether GitLab may
have processed the request; nothing is retried here.
"""

import asyncio

import httpx
from pydantic import BaseModel, ValidationError

from opspilot.agent.ports import CreateOutcome, Issue, TrackerUnavailable
from opspilot.observability import span

# Raised before any request byte can have been sent: provably no side effect.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
TERMINAL_STATUS = {
    400: "gitlab_bad_request",
    401: "gitlab_unauthorized",
    403: "gitlab_forbidden",
    404: "gitlab_not_found",
    409: "gitlab_conflict",
    422: "gitlab_unprocessable",
}


class IssueBody(BaseModel):
    id: int
    iid: int
    web_url: str
    description: str | None = None


class GitLabTracker:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout = timeout
        self.client = httpx.AsyncClient(
            base_url=f"{base_url}/api/v4",
            headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            follow_redirects=False,
            transport=transport,
        )

    async def create_issue(
        self,
        project_id: int,
        title: str,
        description: str,
        labels: tuple[str, ...],
        assignee_ids: tuple[int, ...],
    ) -> CreateOutcome:
        body = {
            "title": title,
            "description": description,
            "labels": ",".join(labels),
            "assignee_ids": list(assignee_ids),
        }
        try:
            with span("gitlab.request"):
                async with asyncio.timeout(self.timeout * 2):
                    response = await self.client.post(f"/projects/{project_id}/issues", json=body)
        except NOT_SENT:
            return CreateOutcome("not_sent", "gitlab_connect_failed", retryable=True)
        except (httpx.HTTPError, TimeoutError):
            # Timeout or broken connection after the request may have been sent.
            return CreateOutcome("unknown", "gitlab_response_lost")
        status = response.status_code
        if 200 <= status < 300:
            try:
                issue = IssueBody.model_validate_json(response.content)
            except ValidationError:
                return CreateOutcome("unknown", "gitlab_malformed_response")
            return CreateOutcome(
                "created",
                "created",
                Issue(issue.id, issue.iid, issue.web_url, issue.description or ""),
            )
        if status == 429:
            return CreateOutcome("rejected", "gitlab_rate_limited", retryable=True)
        if status >= 500:
            # A 5xx may follow a committed write (e.g. gateway timeout): outcome unknown.
            return CreateOutcome("unknown", "gitlab_server_error")
        return CreateOutcome("rejected", TERMINAL_STATUS.get(status, "gitlab_rejected"))

    async def find_by_marker(self, project_id: int, marker: str) -> list[Issue]:
        try:
            with span("gitlab.request"):
                async with asyncio.timeout(self.timeout * 2):
                    response = await self.client.get(
                        f"/projects/{project_id}/issues",
                        params={"search": marker, "in": "description", "per_page": 20},
                    )
        except (httpx.HTTPError, TimeoutError):
            raise TrackerUnavailable from None
        if response.status_code != 200:
            raise TrackerUnavailable
        try:
            issues = [IssueBody.model_validate(item) for item in response.json()]
        except (ValueError, TypeError, ValidationError):
            raise TrackerUnavailable from None
        # GitLab search is fuzzy; require the exact marker in the description.
        return [
            Issue(issue.id, issue.iid, issue.web_url, issue.description or "")
            for issue in issues
            if marker in (issue.description or "")
        ]

    async def close(self) -> None:
        await self.client.aclose()
