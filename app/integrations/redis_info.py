"""Redis INFO adapter using the Redis wire protocol without third-party clients."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import unquote, urlparse

from app.config import config
from app.integrations.base import ExternalAdapterError, adapter_success, require_config


class RedisInfoAdapter:
    """Read Redis INFO plus optional CONFIG/SLOWLOG data over TCP."""

    def __init__(self):
        self.redis_url = config.resolved_redis_url
        self.instance_urls = config.redis_instance_map
        self.timeout_seconds = config.redis_timeout_seconds
        self.allow_admin_commands = config.redis_allow_admin_commands
        self.store_raw_external_payload = config.aiops_store_raw_external_payload

    @property
    def configured(self) -> bool:
        return bool(self.redis_url or self.instance_urls)

    async def query_status(
        self,
        service_name: str,
        redis_instance: str,
        time_range: str,
    ) -> dict[str, Any]:
        target = self._resolve_target(redis_instance)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target["host"], target["port"]),
            timeout=self.timeout_seconds,
        )
        try:
            if target["password"]:
                await self._send_command(reader, writer, "AUTH", target["password"])
            info_text = await self._send_command(reader, writer, "INFO")
            info = self._parse_info(info_text)
            maxclients_text, slowlog_len_text, optional_errors = await self._optional_admin_checks(
                reader,
                writer,
                info,
            )
        finally:
            writer.close()
            await writer.wait_closed()

        maxclients = self._parse_maxclients(maxclients_text)
        missing_fields = self._missing_required_info_fields(info, maxclients)
        if missing_fields:
            raise ExternalAdapterError(
                "Redis INFO response missing required fields: " + ", ".join(missing_fields)
            )

        connected_clients = int(info["connected_clients"])
        usage = connected_clients / maxclients if maxclients else 0.0
        blocked_clients = int(info["blocked_clients"])
        slowlog_len = int(slowlog_len_text or "0")
        big_key_analysis = self._big_key_analysis(info)
        alert_triggered = usage >= 0.9 or slowlog_len > 0 or big_key_analysis["risk_level"] != "low"
        alert_info = {
            "triggered": alert_triggered,
            "message": (
                "Redis connected_clients is close to maxclients"
                if usage >= 0.9
                else (
                    "Redis has slowlog or memory risk"
                    if alert_triggered
                    else "Redis connection usage is normal"
                )
            ),
        }

        return adapter_success(
            source="redis_info",
            summary=f"{redis_instance} connected_clients={connected_clients}/{maxclients}",
            signals={
                "connected_clients": connected_clients,
                "maxclients": maxclients,
                "client_usage_ratio": round(usage, 4),
                "blocked_clients": blocked_clients,
                "slowlog_len": slowlog_len,
                "memory_usage_ratio": big_key_analysis["memory_usage_ratio"],
            },
            raw={
                "info": info if self.store_raw_external_payload else self._compact_info(info),
                "maxclients_response": maxclients_text,
                "slowlog_len": slowlog_len,
            },
            service_name=service_name,
            redis_instance=redis_instance,
            time_range=time_range,
            endpoint=target["display"],
            connected_clients=connected_clients,
            maxclients=maxclients,
            client_usage_ratio=round(usage, 4),
            blocked_clients=blocked_clients,
            used_memory_human=info.get("used_memory_human", ""),
            maxmemory_human=info.get("maxmemory_human", ""),
            slowlog={"len": slowlog_len, "status": "checked"},
            big_key_analysis=big_key_analysis,
            alert_info=alert_info,
            partial_errors=optional_errors,
        )

    async def ping(self, redis_instance: str = "") -> dict[str, Any]:
        """Return a lightweight connectivity check for readiness endpoints."""
        target = self._resolve_target(redis_instance)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target["host"], target["port"]),
            timeout=self.timeout_seconds,
        )
        try:
            if target["password"]:
                await self._send_command(reader, writer, "AUTH", target["password"])
            response = await self._send_command(reader, writer, "PING")
        finally:
            writer.close()
            await writer.wait_closed()
        return {"status": "connected", "message": response or "PONG", "endpoint": target["display"]}

    async def _optional_admin_checks(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        info: dict[str, str],
    ) -> tuple[str, str, list[dict[str, str]]]:
        optional_errors: list[dict[str, str]] = []
        if not self.allow_admin_commands:
            optional_errors.append(
                {
                    "command": "CONFIG GET maxclients/SLOWLOG LEN",
                    "error_message": "disabled by REDIS_ALLOW_ADMIN_COMMANDS=false",
                }
            )
            return str(info.get("maxclients", "0")), "0", optional_errors

        try:
            maxclients_text = await self._send_command(
                reader, writer, "CONFIG", "GET", "maxclients"
            )
        except ExternalAdapterError as exc:
            maxclients_text = str(info.get("maxclients", "0"))
            optional_errors.append({"command": "CONFIG GET maxclients", "error_message": str(exc)})
        try:
            slowlog_len_text = await self._send_command(reader, writer, "SLOWLOG", "LEN")
        except ExternalAdapterError as exc:
            slowlog_len_text = "0"
            optional_errors.append({"command": "SLOWLOG LEN", "error_message": str(exc)})
        return maxclients_text, slowlog_len_text, optional_errors

    async def _send_command(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *parts: str,
    ) -> str:
        command = self._encode_resp(parts)
        writer.write(command)
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=self.timeout_seconds)
        if not line:
            raise ExternalAdapterError("Redis closed connection")
        prefix = line[:1]
        if prefix == b"-":
            raise ExternalAdapterError(line[1:].decode(errors="replace").strip())
        if prefix == b"+":
            return line[1:].decode(errors="replace").strip()
        if prefix == b"$":
            length = int(line[1:].strip())
            if length < 0:
                return ""
            data = await asyncio.wait_for(
                reader.readexactly(length + 2),
                timeout=self.timeout_seconds,
            )
            return data[:-2].decode(errors="replace")
        if prefix == b"*":
            count = int(line[1:].strip())
            values = [await self._read_bulk(reader) for _ in range(count)]
            return "\n".join(values)
        if prefix == b":":
            return line[1:].decode(errors="replace").strip()
        return line.decode(errors="replace").strip()

    async def _read_bulk(self, reader: asyncio.StreamReader) -> str:
        line = await asyncio.wait_for(reader.readline(), timeout=self.timeout_seconds)
        if not line.startswith(b"$"):
            return line.decode(errors="replace").strip()
        length = int(line[1:].strip())
        data = await asyncio.wait_for(reader.readexactly(length + 2), timeout=self.timeout_seconds)
        return data[:-2].decode(errors="replace")

    def _resolve_target(self, redis_instance: str = "") -> dict[str, Any]:
        url = self.instance_urls.get(redis_instance) if redis_instance else ""
        url = require_config(url or self.redis_url, "REDIS_URL or REDIS_HOST")
        parsed = urlparse(url)
        if parsed.scheme and parsed.scheme not in {"redis", "rediss"}:
            raise ExternalAdapterError(f"Unsupported Redis URL scheme: {parsed.scheme}")
        host = parsed.hostname or parsed.path or ""
        if not host:
            raise ExternalAdapterError("Redis host is not configured")
        return {
            "host": host,
            "port": parsed.port or 6379,
            "password": unquote(parsed.password or ""),
            "display": f"{host}:{parsed.port or 6379}",
        }

    @staticmethod
    def _encode_resp(parts: tuple[str, ...]) -> bytes:
        payload = f"*{len(parts)}\r\n"
        for part in parts:
            encoded = part.encode()
            payload += f"${len(encoded)}\r\n{part}\r\n"
        return payload.encode()

    @staticmethod
    def _parse_info(text: str) -> dict[str, str]:
        result = {}
        for line in text.splitlines():
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            result[key] = value
        return result

    @staticmethod
    def _parse_maxclients(text: str) -> int:
        lines = [line for line in text.splitlines() if line and line != "maxclients"]
        return int(lines[0]) if lines else 0

    @staticmethod
    def _missing_required_info_fields(info: dict[str, str], maxclients: int) -> list[str]:
        missing: list[str] = []
        for field_name in ["connected_clients", "blocked_clients"]:
            if field_name not in info:
                missing.append(field_name)
        if maxclients <= 0:
            missing.append("maxclients")
        return missing

    @staticmethod
    def _big_key_analysis(info: dict[str, str]) -> dict[str, Any]:
        used_memory = int(info.get("used_memory", 0) or 0)
        maxmemory = int(info.get("maxmemory", 0) or 0)
        usage = used_memory / maxmemory if maxmemory else 0.0
        db_key_counts = {}
        for key, value in info.items():
            if not key.startswith("db"):
                continue
            first_part = value.split(",", 1)[0]
            if first_part.startswith("keys="):
                db_key_counts[key] = int(first_part.replace("keys=", "") or 0)
        risk_level = "high" if usage >= 0.9 else "medium" if usage >= 0.75 else "low"
        return {
            "status": "not_scanned",
            "reason": "Read-only adapter does not run SCAN to avoid extra production load",
            "risk_level": risk_level,
            "used_memory": used_memory,
            "maxmemory": maxmemory,
            "memory_usage_ratio": round(usage, 4),
            "db_key_counts": db_key_counts,
        }

    @staticmethod
    def _compact_info(info: dict[str, str]) -> dict[str, str]:
        keys = {
            "redis_version",
            "connected_clients",
            "blocked_clients",
            "used_memory",
            "used_memory_human",
            "maxmemory",
            "maxmemory_human",
            "maxclients",
            "instantaneous_ops_per_sec",
            "rejected_connections",
            "expired_keys",
            "evicted_keys",
            "keyspace_hits",
            "keyspace_misses",
        }
        compact = {key: value for key, value in info.items() if key in keys or key.startswith("db")}
        compact["_raw_truncated"] = "true"
        return compact
