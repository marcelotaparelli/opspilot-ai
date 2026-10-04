"""Real HTTP smoke test of a running development stack configured with the fake provider."""

import argparse
import json
import os
from pathlib import Path
from typing import Literal
from urllib.request import Request, urlopen
from uuid import uuid4

from opspilot.api.contracts import DocumentOutput, QueryOutput


def request(
    base_url: str,
    path: str,
    token: str,
    payload: dict[str, object] | None = None,
) -> tuple[int, bytes, str]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    data = json.dumps(payload).encode() if payload is not None else None
    message = Request(base_url + path, data=data, headers=headers)
    with urlopen(message, timeout=10) as response:
        return response.status, response.read(), response.headers["X-Request-ID"]


def smoke(base_url: str, token: str, other_token: str | None = None) -> dict[str, object]:
    if os.environ.get("PROVIDER", "fake") != "fake":
        raise ValueError("smoke requires PROVIDER=fake")
    checks: list[dict[str, object]] = []
    states: tuple[tuple[Literal["/health", "/ready"], str], ...] = (
        ("/health", "ok"),
        ("/ready", "ready"),
    )
    for path, expected in states:
        status, body, correlation = request(base_url, path, "")
        if status != 200 or json.loads(body) != {"status": expected}:
            raise RuntimeError("health/readiness contract failed")
        checks.append({"endpoint": path, "status": status, "request_id": correlation})
    content = f"Smoke runbook {uuid4()}: drain traffic, restart service, verify readiness."
    status, body, correlation = request(
        base_url,
        "/v1/documents",
        token,
        {"content": content, "metadata": {"title": "Smoke runbook", "tags": ["validation"]}},
    )
    document = DocumentOutput.model_validate_json(body)
    if status != 201 or document.request_id != correlation or not document.chunk_ids:
        raise RuntimeError("ingestion contract failed")
    checks.append({"endpoint": "/v1/documents", "status": status, "request_id": correlation})
    status, body, correlation = request(
        base_url, "/v1/query", token, {"question": content, "top_k": 1}
    )
    result = QueryOutput.model_validate_json(body)
    if (
        status != 200
        or result.request_id != correlation
        or result.answer != content
        or not result.citations
        or {item.document_id for item in result.citations} != {document.document_id}
        or {item.document_id for item in result.retrieved_chunks} != {document.document_id}
        or any(item.chunk_id not in document.chunk_ids for item in result.citations)
        or any(item.quote != content for item in result.citations)
    ):
        raise RuntimeError("answer/evidence contract failed")
    checks.append(
        {
            "endpoint": "/v1/query",
            "status": status,
            "request_id": correlation,
            "citation_count": len(result.citations),
            "retrieved_count": len(result.retrieved_chunks),
            "cited_document_id": str(document.document_id),
        }
    )
    if other_token:
        # Second tenant stores identical content; each tenant must only see its own copy.
        status, body, correlation = request(
            base_url,
            "/v1/documents",
            other_token,
            {"content": content, "metadata": {"title": "Smoke runbook"}},
        )
        other = DocumentOutput.model_validate_json(body)
        status, body, correlation = request(
            base_url, "/v1/query", other_token, {"question": content, "top_k": 20}
        )
        result = QueryOutput.model_validate_json(body)
        visible = {item.document_id for item in result.retrieved_chunks + result.citations}
        if status != 200 or document.document_id in visible or other.document_id not in visible:
            raise RuntimeError("cross-tenant isolation contract failed")
        checks.append(
            {
                "endpoint": "/v1/query",
                "status": status,
                "request_id": correlation,
                "check": "second tenant sees only its own document",
                "visible_document_ids": sorted(str(item) for item in visible),
                "first_tenant_document_visible": False,
            }
        )
    return {"provider": "fake", "transport": "HTTP socket", "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    token = os.environ.get("OPSPILOT_TOKEN")
    if not token:
        parser.error("set OPSPILOT_TOKEN to a development tenant credential")
    other_token = os.environ.get("OPSPILOT_OTHER_TOKEN")
    encoded = json.dumps(smoke(args.base_url, token, other_token), indent=2)
    if args.output:
        args.output.write_text(encoded + "\n")
    # Evidence excludes credentials, document text, questions and answers.
    print(encoded)


if __name__ == "__main__":
    main()
