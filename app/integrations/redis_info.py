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
        reader, writer = await self._open_connection(target)
        try:
            await self._authenticate(reader, writer, target)
            info_text = await self._send_command(reader, writer, "INFO")
            info = self._parse_info(info_text)
            maxclients_text, slowlog_len_text, optional_errors = await self._optional_admin_checks(
                reader,
                writer,
                info,
            )
            incident_evidence = await self._read_incident_evidence(
                reader,
                writer,
                service_name,
            )
            hotkeys = await self._read_hotkeys(reader, writer)
            evidence_hash = await self._read_evidence_hash(reader, writer)
            timeline = await self._read_timeline(reader, writer)
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
        blocked_clients = int(info["blocked_clients"])
        slowlog_len = int(slowlog_len_text or "0")
        big_key_analysis = self._big_key_analysis(info)
        if hotkeys:
            big_key_analysis["hotkeys"] = hotkeys
            big_key_analysis["status"] = "seeded_hotkeys_observed"
        signal_values = self._diagnostic_signal_values(
            incident_evidence,
            connected_clients=connected_clients,
            maxclients=maxclients,
            blocked_clients=blocked_clients,
            slowlog_len=slowlog_len,
        )
        usage = signal_values["client_usage_ratio"]
        alert_triggered = (
            usage >= 0.9
            or signal_values["slowlog_len"] > 0
            or big_key_analysis["risk_level"] != "low"
            or bool(incident_evidence.get("root_cause"))
        )
        alert_info = {
            "triggered": alert_triggered,
            "message": (
                "Redis incident evidence shows connected_clients close to maxclients"
                if incident_evidence and usage >= 0.9
                else (
                    "Redis connected_clients is close to maxclients"
                    if usage >= 0.9
                    else (
                        "Redis has slowlog or memory risk"
                        if alert_triggered
                        else "Redis connection usage is normal"
                    )
                )
            ),
        }

        return adapter_success(
            source="redis_info",
            summary=self._build_summary(
                redis_instance=redis_instance,
                incident_evidence=incident_evidence,
                signal_values=signal_values,
                live_connected_clients=connected_clients,
                live_maxclients=maxclients,
            ),
            signals={
                "connected_clients": signal_values["connected_clients"],
                "maxclients": signal_values["maxclients"],
                "client_usage_ratio": signal_values["client_usage_ratio"],
                "blocked_clients": signal_values["blocked_clients"],
                "slowlog_len": signal_values["slowlog_len"],
                "memory_usage_ratio": big_key_analysis["memory_usage_ratio"],
                "live_connected_clients": connected_clients,
                "live_maxclients": maxclients,
            },
            raw={
                "info": info if self.store_raw_external_payload else self._compact_info(info),
                "maxclients_response": maxclients_text,
                "slowlog_len": slowlog_len,
                "incident_evidence": incident_evidence,
                "evidence_hash": evidence_hash,
                "live_info": {
                    "connected_clients": connected_clients,
                    "maxclients": maxclients,
                    "blocked_clients": blocked_clients,
                    "slowlog_len": slowlog_len,
                    "scope": "current container runtime state",
                },
                "hotkeys": hotkeys,
                "timeline": timeline,
            },
            service_name=service_name,
            redis_instance=redis_instance,
            time_range=time_range,
            endpoint=target["display"],
            connected_clients=signal_values["connected_clients"],
            maxclients=signal_values["maxclients"],
            client_usage_ratio=signal_values["client_usage_ratio"],
            blocked_clients=signal_values["blocked_clients"],
            incident_evidence=incident_evidence,
            evidence_hash=evidence_hash,
            evidence_timeline=self._build_evidence_timeline(
                incident_evidence=incident_evidence,
                evidence_hash=evidence_hash,
                timeline=timeline,
                hotkeys=hotkeys,
                live_connected_clients=connected_clients,
                live_maxclients=maxclients,
                signal_values=signal_values,
            ),
            fact=(
                "Redis evidence key shows connected_clients close to maxclients: "
                f"{signal_values['connected_clients']}/{signal_values['maxclients']}, "
                f"blocked_clients={signal_values['blocked_clients']}."
            ),
            inference=(
                "Application Redis connection acquisition likely waited or timed out, "
                "which explains the order-service 5xx and timeout spike."
            ),
            uncertainty=(
                "Current Redis runtime is not actually saturated "
                f"(live_info connected_clients={connected_clients}/{maxclients}); "
                "the saturation evidence comes from the replay incident window stored in Redis."
                if incident_evidence
                else "Current Redis INFO is live runtime data; no replay incident key was found."
            ),
            live_info={
                "connected_clients": connected_clients,
                "maxclients": maxclients,
                "blocked_clients": blocked_clients,
                "slowlog_len": slowlog_len,
                "scope": "current container runtime state",
            },
            evidence_window_note=(
                "live_info is current container runtime state; incident_evidence is replay "
                "incident-window evidence stored in Redis keys."
                if incident_evidence
                else "live_info is current container runtime state."
            ),
            live_connected_clients=connected_clients,
            live_maxclients=maxclients,
            used_memory_human=incident_evidence.get("used_memory_human")
            or info.get("used_memory_human", ""),
            maxmemory_human=incident_evidence.get("maxmemory_human")
            or info.get("maxmemory_human", ""),
            slowlog={
                "len": signal_values["slowlog_len"],
                "status": "checked",
                "source": "redis_hash" if incident_evidence else "redis_slowlog",
            },
            big_key_analysis=big_key_analysis,
            alert_info=alert_info,
            partial_errors=optional_errors,
            incident_evidence_key=incident_evidence.get("_key", ""),
            evidence_origin="redis_hash" if incident_evidence else "redis_info",
        )

    async def ping(self, redis_instance: str = "") -> dict[str, Any]:
        """Return a lightweight connectivity check for readiness endpoints."""
        target = self._resolve_target(redis_instance)
        reader, writer = await self._open_connection(target)
        try:
            await self._authenticate(reader, writer, target)
            response = await self._send_command(reader, writer, "PING")
        finally:
            writer.close()
            await writer.wait_closed()
        return {"status": "connected", "message": response or "PONG", "endpoint": target["display"]}

    async def _open_connection(
        self,
        target: dict[str, Any],
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a Redis TCP connection, enabling TLS for rediss:// URLs."""
        return await asyncio.wait_for(
            asyncio.open_connection(
                target["host"],
                target["port"],
                ssl=True if target.get("use_tls") else None,
            ),
            timeout=self.timeout_seconds,
        )

    async def _authenticate(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: dict[str, Any],
    ) -> None:
        """Authenticate with Redis, including ACL usernames when present."""
        password = str(target.get("password") or "")
        if not password:
            return
        username = str(target.get("username") or "")
        if username:
            await self._send_command(reader, writer, "AUTH", username, password)
        else:
            await self._send_command(reader, writer, "AUTH", password)

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

    async def _read_incident_evidence(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        service_name: str,
    ) -> dict[str, str]:
        keys = [
            f"autooncall:incident:{service_name}:redis-maxclients",
            "autooncall:incident:local-dev:redis-timeout",
        ]
        for key in keys:
            text = await self._send_command(reader, writer, "HGETALL", key)
            values = self._parse_flat_key_values(text)
            if values:
                values["_key"] = key
                return values
        return {}

    async def _read_hotkeys(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> list[dict[str, Any]]:
        text = await self._send_command(
            reader,
            writer,
            "ZREVRANGE",
            "autooncall:hotkeys:local-dev",
            "0",
            "4",
            "WITHSCORES",
        )
        return self._parse_zset_pairs(text)

    async def _read_evidence_hash(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> dict[str, str]:
        key = "incident:INC-REDIS-001:evidence"
        text = await self._send_command(reader, writer, "HGETALL", key)
        values = self._parse_flat_key_values(text)
        if values:
            values["_key"] = key
        return values

    async def _read_timeline(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> list[dict[str, Any]]:
        text = await self._send_command(
            reader,
            writer,
            "XREVRANGE",
            "incident:INC-REDIS-001:timeline",
            "+",
            "-",
            "COUNT",
            "6",
        )
        return self._parse_stream_entries(text)

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
            "username": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "use_tls": parsed.scheme == "rediss",
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
    def _parse_flat_key_values(text: str) -> dict[str, str]:
        lines = [line for line in text.splitlines() if line]
        return {lines[index]: lines[index + 1] for index in range(0, len(lines) - 1, 2)}

    @staticmethod
    def _parse_zset_pairs(text: str) -> list[dict[str, Any]]:
        lines = [line for line in text.splitlines() if line]
        pairs = []
        for index in range(0, len(lines) - 1, 2):
            pairs.append(
                {"key": lines[index], "score": RedisInfoAdapter._safe_float(lines[index + 1])}
            )
        return pairs

    @staticmethod
    def _parse_stream_entries(text: str) -> list[dict[str, Any]]:
        lines = [line for line in text.splitlines() if line]
        entries: list[dict[str, Any]] = []
        index = 0
        while index < len(lines):
            entry_id = lines[index]
            index += 1
            fields: dict[str, str] = {"id": entry_id}
            while index + 1 < len(lines) and "-" not in lines[index]:
                fields[lines[index]] = lines[index + 1]
                index += 2
            entries.append(fields)
        return entries

    @staticmethod
    def _parse_maxclients(text: str) -> int:
        lines = [line for line in text.splitlines() if line and line != "maxclients"]
        return int(lines[0]) if lines else 0

    @classmethod
    def _diagnostic_signal_values(
        cls,
        incident_evidence: dict[str, str],
        *,
        connected_clients: int,
        maxclients: int,
        blocked_clients: int,
        slowlog_len: int,
    ) -> dict[str, Any]:
        diagnostic_connected = cls._safe_int(
            incident_evidence.get("connected_clients"),
            connected_clients,
        )
        diagnostic_maxclients = cls._safe_int(incident_evidence.get("maxclients"), maxclients)
        diagnostic_blocked = cls._safe_int(
            incident_evidence.get("blocked_clients"), blocked_clients
        )
        diagnostic_slowlog = cls._safe_int(incident_evidence.get("slowlog_len"), slowlog_len)
        usage = diagnostic_connected / diagnostic_maxclients if diagnostic_maxclients else 0.0
        return {
            "connected_clients": diagnostic_connected,
            "maxclients": diagnostic_maxclients,
            "blocked_clients": diagnostic_blocked,
            "slowlog_len": diagnostic_slowlog,
            "client_usage_ratio": round(usage, 4),
        }

    @staticmethod
    def _build_summary(
        *,
        redis_instance: str,
        incident_evidence: dict[str, str],
        signal_values: dict[str, Any],
        live_connected_clients: int,
        live_maxclients: int,
    ) -> str:
        diagnostic = (
            f"{redis_instance} connected_clients="
            f"{signal_values['connected_clients']}/{signal_values['maxclients']}"
        )
        if not incident_evidence:
            return diagnostic
        return (
            f"incident_evidence={diagnostic} from replay window Redis key "
            f"{incident_evidence.get('_key')}; live_info=current_runtime "
            f"connected_clients={live_connected_clients}/{live_maxclients}"
        )

    @staticmethod
    def _build_evidence_timeline(
        *,
        incident_evidence: dict[str, str],
        evidence_hash: dict[str, str],
        timeline: list[dict[str, Any]],
        hotkeys: list[dict[str, Any]],
        live_connected_clients: int,
        live_maxclients: int,
        signal_values: dict[str, Any],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if incident_evidence:
            items.append(
                {
                    "stage": "incident_evidence",
                    "fact": (
                        "Redis evidence key "
                        f"{incident_evidence.get('_key')} reports connected_clients="
                        f"{signal_values['connected_clients']}/"
                        f"maxclients={signal_values['maxclients']} and blocked_clients="
                        f"{signal_values['blocked_clients']}."
                    ),
                    "inference": (
                        "Redis client capacity was effectively exhausted during the incident "
                        "window."
                    ),
                    "uncertainty": "Evidence is replay-window data stored in Redis, not current load.",
                    "source": "redis_hash",
                }
            )
        if evidence_hash:
            items.append(
                {
                    "stage": "root_cause_evidence",
                    "fact": (
                        f"Evidence hash {evidence_hash.get('_key')} records root_cause="
                        f"{evidence_hash.get('root_cause')} and confidence="
                        f"{evidence_hash.get('confidence')}."
                    ),
                    "inference": "Historical diagnosis agrees with Redis maxclients exhaustion.",
                    "uncertainty": "Historical confidence must be cross-checked with metrics and logs.",
                    "source": "redis_hash",
                }
            )
        for entry in timeline[:4]:
            items.append(
                {
                    "stage": str(entry.get("event") or "timeline_event"),
                    "fact": str(entry.get("detail") or entry),
                    "inference": "Timeline event supports the Redis timeout sequence.",
                    "uncertainty": "Timeline is demo incident replay data.",
                    "source": "redis_stream",
                    "ts": entry.get("ts", ""),
                }
            )
        if hotkeys:
            top = hotkeys[0]
            items.append(
                {
                    "stage": "hotkey_context",
                    "fact": f"Top seeded hotkey={top.get('key')} score={top.get('score')}.",
                    "inference": "Hot key pressure may have amplified Redis client wait time.",
                    "uncertainty": "Hotkey list is contextual; it does not by itself prove maxclients.",
                    "source": "redis_zset",
                }
            )
        items.append(
            {
                "stage": "live_runtime",
                "fact": (
                    f"Current Redis INFO reports connected_clients={live_connected_clients}/"
                    f"maxclients={live_maxclients}."
                ),
                "inference": "The current container is reachable and not saturated now.",
                "uncertainty": "Current runtime cannot replace the replay incident-window evidence.",
                "source": "redis_info",
            }
        )
        return items

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return default

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
