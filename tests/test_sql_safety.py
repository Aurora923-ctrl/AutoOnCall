"""Tests for bounded dynamic SQL fragment construction."""

import pytest

from app.services.sql_safety import (
    bind_markers,
    trusted_identifier,
    trusted_in_clause,
    trusted_table_statement,
)


def test_bind_markers_only_accepts_known_driver_markers() -> None:
    assert bind_markers(3, "?") == "?, ?, ?"
    assert bind_markers(2, "%s") == "%s, %s"

    with pytest.raises(ValueError, match="at least one"):
        bind_markers(0, "?")
    with pytest.raises(ValueError, match="unsupported"):
        bind_markers(1, ":value")


def test_trusted_identifier_rejects_values_outside_code_owned_whitelist() -> None:
    assert trusted_identifier("trace_events", allowed={"trace_events"}) == "trace_events"
    assert trusted_identifier("event_id", allowed={"event_id"}) == "event_id"

    with pytest.raises(ValueError, match="untrusted SQL identifier"):
        trusted_identifier("trace_events; DROP TABLE trace_events", allowed={"trace_events"})
    with pytest.raises(ValueError, match="untrusted SQL identifier"):
        trusted_identifier("event_id OR 1=1", allowed={"event_id"})


def test_trusted_in_clause_contains_only_driver_bind_markers() -> None:
    assert trusted_in_clause(3, "?") == "(?, ?, ?)"
    assert trusted_in_clause(2, "%s") == "(%s, %s)"


def test_trusted_table_statement_requires_allowlisted_table_and_operation() -> None:
    allowed = {"trace_events"}
    assert (
        trusted_table_statement(
            "SELECT_COUNT",
            table="trace_events",
            allowed_tables=allowed,
            value_count=2,
            marker="?",
        )
        == "SELECT COUNT(*) FROM trace_events WHERE incident_id IN (?, ?)"
    )
    assert (
        trusted_table_statement(
            "DELETE",
            table="trace_events",
            allowed_tables=allowed,
            value_count=1,
            marker="%s",
        )
        == "DELETE FROM trace_events WHERE incident_id IN (%s)"
    )
    assert (
        trusted_table_statement(
            "SELECT_ALL_COUNT",
            table="trace_events",
            allowed_tables=allowed,
        )
        == "SELECT COUNT(*) FROM trace_events"
    )
    assert (
        trusted_table_statement(
            "DELETE_ALL",
            table="trace_events",
            allowed_tables=allowed,
        )
        == "DELETE FROM trace_events"
    )

    with pytest.raises(ValueError, match="untrusted SQL identifier"):
        trusted_table_statement(
            "DELETE",
            table="trace_events; DROP TABLE trace_events",
            allowed_tables=allowed,
            value_count=1,
            marker="?",
        )
