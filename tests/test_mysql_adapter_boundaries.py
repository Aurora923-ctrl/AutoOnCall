"""Boundary tests for MySQL metric semantics and public payloads."""

from __future__ import annotations

import pytest

from app.integrations.base import ExternalAdapterNotFoundError
from app.integrations.mysql import MySQLStatusAdapter


def test_mysql_server_and_application_pool_metrics_are_separate() -> None:
    evidence = MySQLStatusAdapter._parse_observed_value(
        "slow_queries=18,pool_waiting=6,active_connections=188/200"
    )

    assert MySQLStatusAdapter._pool_active_connections(evidence) == 188
    assert MySQLStatusAdapter._connection_pool_max(evidence) == 200
    assert (
        MySQLStatusAdapter._max_connections([{"Variable_name": "max_connections", "Value": "151"}])
        == 151
    )


def test_mysql_public_processlist_omits_identity_and_sql_text() -> None:
    rows = [
        {
            "Id": 12,
            "User": "root",
            "Host": "mysql.internal:3306",
            "db": "payments",
            "Command": "Query",
            "Time": 3,
            "State": "executing",
            "Info": "SELECT secret FROM payment_cards",
        }
    ]

    assert MySQLStatusAdapter._public_processlist(rows) == [
        {
            "Command": "Query",
            "Time": 3,
            "State": "executing",
            "has_statement": True,
        }
    ]


def test_mysql_unknown_named_instance_does_not_fall_back_to_default() -> None:
    adapter = MySQLStatusAdapter()
    adapter.dsn = "mysql+pymysql://user:password@default-mysql:3306/app"
    adapter.instance_dsns = {
        "payment-mysql": "mysql+pymysql://user:password@payment-mysql:3306/app"
    }

    with pytest.raises(ExternalAdapterNotFoundError, match="payment-mysql-typo"):
        adapter._resolve_dsn("payment-mysql-typo")


def test_mysql_named_instance_requires_explicit_instance_map() -> None:
    adapter = MySQLStatusAdapter()
    adapter.dsn = "mysql+pymysql://user:password@default-mysql:3306/app"
    adapter.instance_dsns = {}

    with pytest.raises(ExternalAdapterNotFoundError, match="payment-mysql"):
        adapter._resolve_dsn("payment-mysql")


def test_mysql_dsn_parser_handles_ipv6_encoded_credentials_database_and_options() -> None:
    kwargs = MySQLStatusAdapter._connection_kwargs(
        "mysql+pymysql://user:p%40ss%3Aword@[2001:db8::1]:3307/"
        "payments%2Farchive?charset=utf8mb4&ssl_ca=%2Fetc%2Fmysql%2Fca.pem"
    )

    assert kwargs == {
        "host": "2001:db8::1",
        "port": 3307,
        "user": "user",
        "password": "p@ss:word",
        "database": "payments/archive",
        "charset": "utf8mb4",
        "ssl": {"ca": "/etc/mysql/ca.pem"},
    }
