"""
MCP 客户端管理
提供全局单例的 MCP 客户端，避免重复初始化
"""

import asyncio
import errno
import hashlib
import inspect
import json
import re
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from loguru import logger
from mcp.types import CallToolResult, TextContent

from app.config import config

_mcp_client: MultiServerMCPClient | None = None
_mcp_client_cache_key: str | None = None
_mcp_client_lock: asyncio.Lock | None = None
_mcp_client_lock_loop: asyncio.AbstractEventLoop | None = None
_mcp_active_tasks: set[asyncio.Task[Any]] = set()
_mcp_closing = False
READ_ONLY_MCP_TOOL_NAMES = frozenset(
    {
        "get_current_timestamp",
        "get_region_code_by_name",
        "get_topic_info_by_name",
        "query_cpu_metrics",
        "query_memory_metrics",
        "search_log",
        "search_topic_by_service_name",
    }
)
DEFAULT_MCP_CALL_TIMEOUT_SECONDS = 12.0
_SAFE_MCP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


class MCPCallBudgetTimeout(TimeoutError):
    """Raised when the interceptor's total MCP call budget is exhausted."""


async def retry_interceptor(
    request: MCPToolCallRequest,
    handler,
    max_retries: int = 3,
    delay: float = 1.0,
    timeout_seconds: float = DEFAULT_MCP_CALL_TIMEOUT_SECONDS,
):
    """MCP 工具调用重试拦截器

    当工具调用失败时，使用指数退避策略自动重试。
    如果所有重试都失败，返回包含错误信息的结果而不是抛出异常。

    MCPToolCallRequest 结构：
    - name: str - 工具名称
    - args: dict[str, Any] - 工具参数
    - server_name: str - 服务器名称

    Args:
        request: MCP 工具调用请求
        handler: 实际的工具调用处理器
        max_retries: 最大重试次数（默认3次）
        delay: 初始延迟时间（秒，默认1秒）

    Returns:
        CallToolResult: 工具调用结果或错误信息
    """
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")
    if delay < 0:
        raise ValueError("delay must be non-negative")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0")

    last_error = None
    read_only = request.name in READ_ONLY_MCP_TOOL_NAMES
    max_attempts = max_retries if read_only else 1
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    for attempt in range(max_attempts):
        task: asyncio.Task[Any] | None = None
        try:
            logger.info(
                f"调用 MCP 工具: {request.name} "
                f"(服务器: {request.server_name}, 第 {attempt + 1}/{max_retries} 次尝试)"
            )

            started = asyncio.get_running_loop().create_future()

            async def invoke_handler(started_signal=started):
                started_signal.set_result(None)
                return await handler(request)

            task = asyncio.create_task(invoke_handler())
            _track_mcp_task(task)
            await started
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise MCPCallBudgetTimeout("MCP tool total timeout exhausted")
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise MCPCallBudgetTimeout("MCP tool total timeout exhausted")
            result = task.result()
            logger.info(f"MCP 工具 {request.name} 调用成功")
            return result

        except asyncio.CancelledError:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        except Exception as e:
            last_error = e
            safe_error = _safe_exception_label(e)
            logger.warning(
                f"MCP 工具 {request.name} 调用失败 "
                f"(第 {attempt + 1}/{max_retries} 次): {safe_error}"
            )

            if (
                isinstance(e, MCPCallBudgetTimeout)
                or not read_only
                or not _is_retryable_mcp_error(e)
            ):
                break
            if attempt < max_attempts - 1:
                wait_time = delay * (2**attempt)  # 指数退避
                logger.info(f"等待 {wait_time:.1f} 秒后重试...")
                if asyncio.get_running_loop().time() + wait_time >= deadline:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= wait_time:
                    break
                await asyncio.sleep(wait_time)

    attempts = attempt + 1
    error_msg = (
        f"工具 {request.name} 调用失败，attempts={attempts}: {_safe_exception_label(last_error)}"
    )
    logger.error(error_msg)
    return CallToolResult(content=[TextContent(type="text", text=error_msg)], isError=True)


def select_safe_mcp_tools(tools: list[Any] | None) -> list[Any]:
    """Expose only duplicate-free, explicitly read-only MCP tools to model-facing agents."""
    from app.tools.base import select_named_tools

    selected = select_named_tools(tools, READ_ONLY_MCP_TOOL_NAMES)
    for tool in selected:
        name = str(getattr(tool, "name", "") or "")
        if not _SAFE_MCP_NAME_RE.fullmatch(name):
            raise ValueError(f"Unsafe MCP tool name: {name}")
        schema = getattr(tool, "args_schema", None)
        if isinstance(schema, dict) and schema.get("type") not in (None, "object"):
            raise ValueError(f"MCP tool {name} must expose an object input schema")
    return selected


async def discover_safe_mcp_tools(
    client: MultiServerMCPClient,
    *,
    timeout_seconds: float | None = None,
) -> list[Any]:
    """Discover and allowlist MCP tools within one bounded timeout."""
    timeout = (
        timeout_seconds if timeout_seconds is not None else config.mcp_discovery_timeout_seconds
    )
    if timeout <= 0:
        raise ValueError("timeout_seconds must be greater than 0")
    task = asyncio.create_task(client.get_tools())
    _track_mcp_task(task)
    try:
        tools = await asyncio.wait_for(task, timeout=timeout)
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise
    except TimeoutError:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise
    return select_safe_mcp_tools(tools)


async def get_mcp_client(
    servers: dict[str, dict[str, str]] | None = None,
    tool_interceptors: list | None = None,
    force_new: bool = False,
) -> MultiServerMCPClient:
    """
    获取或初始化 MCP 客户端（不带重试拦截器）

    这是一个单例模式，确保整个应用只有一个 MCP 客户端实例（除非 force_new=True）

    从 langchain-mcp-adapters 0.1.0 开始，MultiServerMCPClient 不再支持作为上下文管理器使用。
    直接创建实例即可使用。

    Args:
        servers: MCP 服务器配置，默认使用 DEFAULT_MCP_SERVERS
        tool_interceptors: 自定义工具拦截器列表
        force_new: 是否强制创建新实例（用于特殊场景，如需要不同配置）

    Returns:
        MultiServerMCPClient: MCP 客户端实例
    """
    global _mcp_client, _mcp_client_cache_key
    if _mcp_closing:
        raise RuntimeError("MCP client is closing")

    if force_new:
        logger.info("创建新的 MCP 客户端实例（非单例）")
        client = _create_mcp_client(servers or config.mcp_servers, tool_interceptors)
        # 不再需要 __aenter__()，直接返回即可
        return client

    effective_servers = servers or config.mcp_servers
    cache_key = _mcp_client_key(effective_servers, tool_interceptors)
    lock = _get_mcp_client_lock()
    async with lock:
        if _mcp_closing:
            raise RuntimeError("MCP client is closing")
        if _mcp_client is None:
            logger.info("初始化全局 MCP 客户端...")
            _mcp_client = _create_mcp_client(effective_servers, tool_interceptors)
            _mcp_client_cache_key = cache_key
            logger.info("全局 MCP 客户端初始化完成")
        elif _mcp_client_cache_key != cache_key:
            raise RuntimeError(
                "Global MCP client is already initialized with different servers or interceptors; "
                "use force_new=True for an isolated client"
            )

    return _mcp_client


async def get_mcp_client_with_retry(
    servers: dict[str, dict[str, str]] | None = None,
    tool_interceptors: list | None = None,
    force_new: bool = False,
) -> MultiServerMCPClient:
    """
    获取或初始化带重试功能的 MCP 客户端

    这是一个单例模式，确保整个应用只有一个 MCP 客户端实例（除非 force_new=True）
    重试拦截器会自动添加到拦截器列表的开头

    Args:
        servers: MCP 服务器配置，默认使用 DEFAULT_MCP_SERVERS
        tool_interceptors: 自定义工具拦截器列表（会在重试拦截器之后添加）
        force_new: 是否强制创建新实例（用于特殊场景，如需要不同配置）

    Returns:
        MultiServerMCPClient: 带重试功能的 MCP 客户端实例
    """
    interceptors = [retry_interceptor]

    if tool_interceptors:
        interceptors.extend(tool_interceptors)

    return await get_mcp_client(
        servers=servers, tool_interceptors=interceptors, force_new=force_new
    )


def _create_mcp_client(
    servers: dict[str, dict[str, str]], tool_interceptors: list | None = None
) -> MultiServerMCPClient:
    """
    创建 MCP 客户端实例

    Args:
        servers: MCP 服务器配置
        tool_interceptors: 工具拦截器列表

    Returns:
        MultiServerMCPClient: 未初始化的客户端实例
    """
    kwargs: dict[str, Any] = {}

    if tool_interceptors:
        kwargs["tool_interceptors"] = tool_interceptors

    return MultiServerMCPClient(servers, **kwargs)  # type: ignore[arg-type]


def _get_mcp_client_lock() -> asyncio.Lock:
    global _mcp_client_lock, _mcp_client_lock_loop
    loop = asyncio.get_running_loop()
    if _mcp_client_lock is None or _mcp_client_lock_loop is not loop:
        _mcp_client_lock = asyncio.Lock()
        _mcp_client_lock_loop = loop
    return _mcp_client_lock


def _mcp_client_key(
    servers: dict[str, dict[str, str]],
    tool_interceptors: list | None,
) -> str:
    interceptor_names = [_interceptor_identity(item) for item in tool_interceptors or []]
    return json.dumps(
        {
            "servers": servers,
            "interceptors": interceptor_names,
        },
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def _interceptor_identity(item: Any) -> str:
    module = getattr(item, "__module__", type(item).__module__)
    qualname = getattr(item, "__qualname__", type(item).__qualname__)
    if inspect.isfunction(item) or inspect.ismethod(item):
        return f"{module}.{qualname}"
    state = getattr(item, "__dict__", {})
    state_json = json.dumps(state, ensure_ascii=True, sort_keys=True, default=str)
    state_hash = hashlib.sha256(state_json.encode("utf-8")).hexdigest()[:12]
    return f"{module}.{qualname}#{state_hash}"


async def close_mcp_client(client: MultiServerMCPClient | None = None) -> None:
    """Close an isolated client or release the cached global client."""
    global _mcp_client, _mcp_client_cache_key, _mcp_client_lock, _mcp_client_lock_loop
    global _mcp_closing
    closing_global = client is None
    lock: asyncio.Lock | None = None
    if closing_global:
        _mcp_closing = True
        lock = _get_mcp_client_lock()
    try:
        if lock is not None:
            await lock.acquire()
        if client is None:
            client = _mcp_client
            _mcp_client = None
            _mcp_client_cache_key = None
        if closing_global:
            active = [task for task in tuple(_mcp_active_tasks) if not task.done()]
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
        if client is None:
            return
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close is None:
            return
        value = close()
        if inspect.isawaitable(value):
            await value
    finally:
        if closing_global:
            if lock is not None and lock.locked():
                lock.release()
            _mcp_client_lock = None
            _mcp_client_lock_loop = None
            _mcp_closing = False


def _track_mcp_task(task: asyncio.Task[Any]) -> None:
    _mcp_active_tasks.add(task)
    task.add_done_callback(_mcp_active_tasks.discard)


def _safe_exception_label(exc: BaseException | None) -> str:
    if exc is None:
        return "unknown_error"
    raw = str(exc)
    fingerprint = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{type(exc).__name__} (error_id={fingerprint})"


def _is_retryable_mcp_error(exc: BaseException) -> bool:
    if isinstance(exc, (PermissionError, ValueError, TypeError, LookupError)):
        return False
    if isinstance(exc, asyncio.TimeoutError | TimeoutError | ConnectionError):
        return True
    if isinstance(exc, OSError):
        return exc.errno in {
            None,
            errno.ECONNABORTED,
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.ENETDOWN,
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
            errno.ETIMEDOUT,
        }
    text = str(exc).lower()
    if any(
        marker in text
        for marker in ("permission", "forbidden", "unauthorized", "invalid argument", "not found")
    ):
        return False
    return any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "connection",
            "temporarily unavailable",
            "server error",
        )
    )
