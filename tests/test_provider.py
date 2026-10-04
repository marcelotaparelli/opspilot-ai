import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from opspilot.chunking import split_document
from opspilot.config import Settings
from opspilot.domain import Document, Metadata, ProviderError
from opspilot.providers.openai import OpenAIProvider
from tests.helpers import MALICIOUS, TENANT_A


def mock_provider(settings: Settings, handler: httpx.AsyncBaseTransport) -> OpenAIProvider:
    config = settings.model_copy(
        update={"provider": "openai", "openai_api_key": SecretStr("mock-key")}
    )
    client = AsyncOpenAI(
        api_key="mock-key",
        max_retries=0,
        timeout=0.1,
        http_client=httpx.AsyncClient(transport=handler),
    )
    return OpenAIProvider(config, client)


@pytest.mark.parametrize("status", [401, 429, 500, 503])
async def test_sdk_errors_are_controlled_without_retries(settings: Settings, status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"message": "SECRET_PROVIDER_DETAIL"}})

    provider = mock_provider(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as error:
            await provider.embed(["sensitive input"])
        assert "SECRET_PROVIDER_DETAIL" not in str(error.value)
        assert calls == 1
    finally:
        await provider.close()


async def test_provider_deadline(settings: Settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    settings = settings.model_copy(update={"provider_timeout_seconds": 0.01})
    provider = mock_provider(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError):
            await provider.embed(["input"])
    finally:
        await provider.close()


@pytest.mark.parametrize("malformed", [False, True])
async def test_embedding_schema_dimensions_and_order(settings: Settings, malformed: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["dimensions"] == 256
        vector = [1.0] * (255 if malformed else 256)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": vector}],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    provider = mock_provider(settings, httpx.MockTransport(handler))
    try:
        if malformed:
            with pytest.raises(ProviderError):
                await provider.embed(["text"])
        else:
            assert len((await provider.embed(["text"]))[0]) == 256
    finally:
        await provider.close()


@pytest.mark.parametrize("invalid", [False, True])
async def test_structured_output_uses_sdk_schema_and_untrusted_evidence(
    settings: Settings, invalid: bool
) -> None:
    chunk = split_document(Document(uuid4(), TENANT_A, MALICIOUS, Metadata("test")))[0]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["text"]["format"]["type"] == "json_schema"
        assert payload["text"]["format"]["strict"] is True
        assert payload["store"] is False
        assert "UNTRUSTED DATA" in payload["instructions"]
        assert MALICIOUS not in payload["instructions"]
        assert "untrusted_evidence" in payload["input"]
        answer = {"answer": "Evidence", "cited_chunk_ids": [str(chunk.id)]}
        if invalid:
            answer["unexpected_permission"] = "admin"
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-4.1-mini",
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
                "output": [
                    {
                        "type": "message",
                        "id": "msg_test",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": json.dumps(answer), "annotations": []}
                        ],
                    }
                ],
            },
        )

    provider = mock_provider(settings, httpx.MockTransport(handler))
    try:
        if invalid:
            with pytest.raises(ProviderError):
                await provider.answer("question", (chunk,))
        else:
            result = await provider.answer("question", (chunk,))
            assert result.cited_chunk_ids == (chunk.id,)
    finally:
        await provider.close()


@pytest.mark.parametrize("failure", ["unsupported_schema", "invalid_json", "refusal", "timeout"])
async def test_answer_transport_failures_are_controlled(settings: Settings, failure: str) -> None:
    chunk = split_document(Document(uuid4(), TENANT_A, "evidence", Metadata("test")))[0]
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1/responses"
        if failure == "timeout":
            await asyncio.sleep(5)
        if failure == "unsupported_schema":
            return httpx.Response(
                400,
                json={"error": {"message": "Unsupported schema", "code": "unsupported_schema"}},
            )
        content: dict[str, object] = (
            {"type": "refusal", "refusal": "Cannot answer"}
            if failure == "refusal"
            else {"type": "output_text", "text": "{invalid JSON", "annotations": []}
        )
        return httpx.Response(
            200,
            json={
                "id": "resp_failure",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-4.1-mini",
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
                "output": [
                    {
                        "type": "message",
                        "id": "msg_failure",
                        "role": "assistant",
                        "status": "completed",
                        "content": [content],
                    }
                ],
            },
        )

    settings = settings.model_copy(update={"provider_timeout_seconds": 0.05})
    provider = mock_provider(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError):
            await provider.answer("question", (chunk,))
        assert calls == 1
    finally:
        await provider.close()


@pytest.mark.parametrize("indices", [(1, 0), (0, 0), (2, 0)])
async def test_embedding_batch_indices_are_validated_and_reordered(
    settings: Settings, indices: tuple[int, int]
) -> None:
    first, second = [1.0] * 256, [2.0] * 256

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["input"] == ["first chunk", "second chunk"]
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"object": "embedding", "index": indices[0], "embedding": second},
                    {"object": "embedding", "index": indices[1], "embedding": first},
                ],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )

    provider = mock_provider(settings, httpx.MockTransport(handler))
    try:
        if indices == (1, 0):
            assert await provider.embed(["first chunk", "second chunk"]) == [first, second]
        else:
            with pytest.raises(ProviderError):
                await provider.embed(["first chunk", "second chunk"])
    finally:
        await provider.close()


def test_production_client_has_bounded_timeout_and_no_retries(settings: Settings) -> None:
    config = settings.model_copy(
        update={
            "provider": "openai",
            "openai_api_key": SecretStr("mock-key"),
            "provider_timeout_seconds": 7.5,
        }
    )
    provider = OpenAIProvider(config)
    assert provider.client.max_retries == 0
    assert provider.client.timeout == 7.5


@pytest.mark.parametrize("operation", ["embed", "answer"])
async def test_network_failure_is_controlled(settings: Settings, operation: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("SECRET_NETWORK_DETAIL", request=request)

    chunk = split_document(Document(uuid4(), TENANT_A, "evidence", Metadata("test")))[0]
    provider = mock_provider(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as error:
            if operation == "embed":
                await provider.embed(["text"])
            else:
                await provider.answer("question", (chunk,))
        assert "SECRET_NETWORK_DETAIL" not in str(error.value) and calls == 1
    finally:
        await provider.close()


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
async def test_answer_http_errors_are_controlled_without_retries(
    settings: Settings, status: int
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"message": "SECRET_PROVIDER_DETAIL"}})

    chunk = split_document(Document(uuid4(), TENANT_A, "evidence", Metadata("test")))[0]
    provider = mock_provider(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as error:
            await provider.answer("question", (chunk,))
        assert "SECRET_PROVIDER_DETAIL" not in str(error.value) and calls == 1
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "answer",
    [
        {"answer": "x", "cited_chunk_ids": ["not-a-uuid"]},
        {"answer": "x"},
        {"cited_chunk_ids": []},
        {"answer": "", "cited_chunk_ids": []},
        {"answer": 1, "cited_chunk_ids": []},
        {"answer": "x", "cited_chunk_ids": "not-a-list"},
        ["answer", "x"],
    ],
)
async def test_incompatible_structured_output_is_rejected(
    settings: Settings, answer: object
) -> None:
    chunk = split_document(Document(uuid4(), TENANT_A, "evidence", Metadata("test")))[0]

    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "id": "resp_bad",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "gpt-4.1-mini",
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "output": [
                {
                    "type": "message",
                    "id": "msg_bad",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": json.dumps(answer), "annotations": []}
                    ],
                }
            ],
        }
        return httpx.Response(200, json=body)

    provider = mock_provider(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError):
            await provider.answer("question", (chunk,))
    finally:
        await provider.close()


async def test_any_sdk_error_maps_to_provider_error(settings: Settings) -> None:
    from openai import ContentFilterFinishReasonError

    class Responses:
        async def parse(self, **_: object) -> None:
            # Subclass of OpenAIError but not APIError: must not escape as a 500.
            raise ContentFilterFinishReasonError()

    class Client:
        responses = Responses()

    config = settings.model_copy(
        update={"provider": "openai", "openai_api_key": SecretStr("mock-key")}
    )
    provider = OpenAIProvider(config, Client())  # type: ignore[arg-type]  # minimal SDK double
    chunk = split_document(Document(uuid4(), TENANT_A, "evidence", Metadata("test")))[0]
    with pytest.raises(ProviderError):
        await provider.answer("question", (chunk,))
