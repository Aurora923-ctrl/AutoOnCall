"""Boundary tests for MCP client retries and singleton lifecycle."""

from __future__ import annotations

import asyncio

import pytest
from mcp.types import TextContent

from app.agent import mcp_client as mcp_client_module


class FakeRequest:
    name = "query_cpu_metrics"
    args: dict[str, object] = {}
    server_name = "monitor"


class UnsafeRequest:
    name = "restart_service"
    args: dict[str, object] = {}
    server_name = "operations"


@pytest.mark.asyncio
async def test_retry_interceptor_propagates_cancellation(monkeypatch) -> None:
    sleep_calls = 0

    async def fake_sleep(_: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1

    async def cancelled_handler(_: object) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await mcp_client_module.retry_interceptor(
            FakeRequest(),
            cancelled_handler,
            max_retries=3,
            delay=0,
        )

    assert sleep_calls == 0


@pytest.mark.asyncio
async def test_retry_interceptor_does_not_return_raw_exception_secrets() -> None:
    async def failing_handler(_: object) -> object:
        raise RuntimeError("Authorization: Bearer super-secret-token")

    result = await mcp_client_module.retry_interceptor(
        FakeRequest(),
        failing_handler,
        max_retries=1,
        delay=0,
    )

    assert result.isError is True
    assert isinstance(result.content[0], TextContent)
    assert "super-secret-token" not in result.content[0].text
    assert "RuntimeError" in result.content[0].text
    assert "error_id=" in result.content[0].text


@pytest.mark.asyncio
async def test_retry_interceptor_does_not_retry_permission_errors(monkeypatch) -> None:
    calls = 0
    sleeps = 0

    async def failing_handler(_: object) -> object:
        nonlocal calls
        calls += 1
        raise PermissionError("forbidden secret endpoint")

    async def fake_sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await mcp_client_module.retry_interceptor(
        FakeRequest(),
        failing_handler,
        max_retries=3,
        delay=1,
    )

    assert result.isError is True
    assert calls == 1
    assert sleeps == 0
    assert "secret endpoint" not in result.content[0].text


@pytest.mark.asyncio
async def test_retry_interceptor_rejects_invalid_retry_configuration() -> None:
    async def handler(_: object) -> object:
        return object()

    with pytest.raises(ValueError, match="at least 1"):
        await mcp_client_module.retry_interceptor(
            FakeRequest(),
            handler,
            max_retries=0,
        )
    with pytest.raises(ValueError, match="non-negative"):
        await mcp_client_module.retry_interceptor(
            FakeRequest(),
            handler,
            delay=-1,
        )
    with pytest.raises(ValueError, match="greater than 0"):
        await mcp_client_module.retry_interceptor(
            FakeRequest(),
            handler,
            timeout_seconds=0,
        )


@pytest.mark.asyncio
async def test_retry_interceptor_never_retries_non_allowlisted_tools(monkeypatch) -> None:
    calls = 0
    sleeps = 0

    async def failing_handler(_: object) -> object:
        nonlocal calls
        calls += 1
        raise ConnectionError("write outcome is unknown")

    async def fake_sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await mcp_client_module.retry_interceptor(
        UnsafeRequest(),
        failing_handler,
        max_retries=3,
        delay=0,
    )

    assert result.isError is True
    assert calls == 1
    assert sleeps == 0


@pytest.mark.asyncio
async def test_retry_interceptor_enforces_total_timeout_budget() -> None:
    calls = 0

    async def slow_handler(_: object) -> object:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return object()

    result = await mcp_client_module.retry_interceptor(
        FakeRequest(),
        slow_handler,
        max_retries=3,
        delay=0,
        timeout_seconds=0.01,
    )

    assert result.isError is True
    assert calls == 1
    assert "MCPCallBudgetTimeout" in result.content[0].text


@pytest.mark.asyncio
async def test_mcp_tool_discovery_is_bounded_by_timeout() -> None:
    class SlowClient:
        async def get_tools(self) -> list[object]:
            await asyncio.sleep(0.1)
            return []

    with pytest.raises(TimeoutError):
        await mcp_client_module.discover_safe_mcp_tools(
            SlowClient(),
            timeout_seconds=0.01,
        )


def test_safe_mcp_tool_selection_rejects_duplicates_and_filters_actions() -> None:
    class Tool:
        def __init__(self, name: str) -> None:
            self.name = name

    selected = mcp_client_module.select_safe_mcp_tools(
        [Tool("query_cpu_metrics"), Tool("restart_service")]
    )
    assert [tool.name for tool in selected] == ["query_cpu_metrics"]

    with pytest.raises(ValueError, match="Duplicate discovered tool name"):
        mcp_client_module.select_safe_mcp_tools(
            [Tool("query_cpu_metrics"), Tool("query_cpu_metrics")]
        )


def test_mcp_client_cache_key_reuses_equivalent_interceptor_instances() -> None:
    class StatefulInterceptor:
        async def __call__(self, request: object, handler: object) -> object:
            return await handler(request)

    first = StatefulInterceptor()
    second = StatefulInterceptor()
    servers = {"one": {"transport": "stdio"}}

    assert mcp_client_module._mcp_client_key(
        servers,
        [first],
    ) == mcp_client_module._mcp_client_key(
        servers,
        [second],
    )


def test_mcp_client_cache_key_distinguishes_interceptor_state() -> None:
    class StatefulInterceptor:
        def __init__(self, mode: str) -> None:
            self.mode = mode

    servers = {"one": {"transport": "stdio"}}

    assert mcp_client_module._mcp_client_key(
        servers,
        [StatefulInterceptor("strict")],
    ) != mcp_client_module._mcp_client_key(
        servers,
        [StatefulInterceptor("permissive")],
    )


@pytest.mark.asyncio
async def test_mcp_singleton_rejects_incompatible_configuration(monkeypatch) -> None:
    created: list[object] = []

    def fake_create(servers: dict, tool_interceptors: list | None = None) -> object:
        client = object()
        created.append(client)
        return client

    monkeypatch.setattr(mcp_client_module, "_mcp_client", None)
    monkeypatch.setattr(mcp_client_module, "_mcp_client_cache_key", None)
    monkeypatch.setattr(mcp_client_module, "_mcp_client_lock", None)
    monkeypatch.setattr(mcp_client_module, "_mcp_client_lock_loop", None)
    monkeypatch.setattr(mcp_client_module, "_create_mcp_client", fake_create)

    first = await mcp_client_module.get_mcp_client(servers={"one": {"transport": "stdio"}})

    with pytest.raises(RuntimeError, match="different servers or interceptors"):
        await mcp_client_module.get_mcp_client(servers={"two": {"transport": "stdio"}})

    assert first is created[0]
    assert len(created) == 1


@pytest.mark.asyncio
async def test_mcp_singleton_initialization_is_serialized(monkeypatch) -> None:
    created: list[object] = []

    def fake_create(servers: dict, tool_interceptors: list | None = None) -> object:
        client = object()
        created.append(client)
        return client

    monkeypatch.setattr(mcp_client_module, "_mcp_client", None)
    monkeypatch.setattr(mcp_client_module, "_mcp_client_cache_key", None)
    monkeypatch.setattr(mcp_client_module, "_mcp_client_lock", None)
    monkeypatch.setattr(mcp_client_module, "_mcp_client_lock_loop", None)
    monkeypatch.setattr(mcp_client_module, "_create_mcp_client", fake_create)
    servers = {"one": {"transport": "stdio"}}

    clients = await asyncio.gather(
        *(mcp_client_module.get_mcp_client(servers=servers) for _ in range(8))
    )

    assert len(created) == 1
    assert all(client is clients[0] for client in clients)


@pytest.mark.asyncio
async def test_mcp_singleton_lock_is_recreated_for_a_new_event_loop(monkeypatch) -> None:
    monkeypatch.setattr(mcp_client_module, "_mcp_client_lock", asyncio.Lock())
    monkeypatch.setattr(mcp_client_module, "_mcp_client_lock_loop", object())

    lock = mcp_client_module._get_mcp_client_lock()

    assert lock is mcp_client_module._mcp_client_lock
    assert mcp_client_module._mcp_client_lock_loop is asyncio.get_running_loop()


@pytest.mark.asyncio
async def test_close_mcp_client_releases_and_closes_cached_client(monkeypatch) -> None:
    class CloseableClient:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    client = CloseableClient()
    monkeypatch.setattr(mcp_client_module, "_mcp_client", client)
    monkeypatch.setattr(mcp_client_module, "_mcp_client_cache_key", "cached")
    monkeypatch.setattr(mcp_client_module, "_mcp_client_lock", asyncio.Lock())
    monkeypatch.setattr(mcp_client_module, "_mcp_client_lock_loop", asyncio.get_running_loop())

    await mcp_client_module.close_mcp_client()

    assert client.closed is True
    assert mcp_client_module._mcp_client is None
    assert mcp_client_module._mcp_client_cache_key is None
    assert mcp_client_module._mcp_client_lock is None
    assert mcp_client_module._mcp_client_lock_loop is None


@pytest.mark.asyncio
async def test_close_mcp_client_closes_isolated_client_without_resetting_global(
    monkeypatch,
) -> None:
    class CloseableClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    global_client = object()
    isolated_client = CloseableClient()
    monkeypatch.setattr(mcp_client_module, "_mcp_client", global_client)
    monkeypatch.setattr(mcp_client_module, "_mcp_client_cache_key", "cached")

    await mcp_client_module.close_mcp_client(isolated_client)

    assert isolated_client.closed is True
    assert mcp_client_module._mcp_client is global_client
    assert mcp_client_module._mcp_client_cache_key == "cached"
