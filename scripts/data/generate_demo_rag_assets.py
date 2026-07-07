"""Generate multi-source RAG demo assets for the interview golden chains."""

from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "aiops-docs"


REDIS_PDF_LINES = [
    "Redis Maxclients Postmortem",
    "Incident: order-service Redis connection timeout and 5xx spike.",
    "Incident window: 2026-07-06 10:00-10:18 UTC.",
    "Evidence: connected_clients=9940, maxclients=10000, blocked_clients=37.",
    "Prometheus showed 5xx and P95 latency rising during the same window.",
    "Loki logs showed Redis timeout and connection-pool wait messages.",
    "Root cause: Redis client capacity was exhausted by retry amplification.",
    "Remediation boundary: increasing maxclients, restarting Redis, or changing pool",
    "settings requires human approval and a production change window.",
]

PAYMENT_WIKI_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Payment Runbook - MySQL Slow Query</title>
</head>
<body>
  <h1>Payment Runbook</h1>
  <h2>MySQL 慢查询</h2>
  <p>payment-service 出现 checkout latency 时，先确认 slow query digest、active
  connections、pool_waiting 和最近发布记录。</p>
  <p>当前 golden case 的关键事实是 slow_queries=18、active_connections=188/200、
  pool_waiting=6，说明慢 SQL 持有连接并放大连接池等待。</p>
  <p>定位 SQL 后需要执行 EXPLAIN；新增索引、修改 SQL、调整连接池或重启服务都必须进入人工审批和变更窗口。</p>
  <h2>Redis 依赖超时</h2>
  <p>如果支付链路依赖 Redis，也要交叉检查 Redis timeout 日志，避免把下游缓存耗尽误判成数据库根因。</p>
</body>
</html>
"""

TICKET_ROWS = [
    {
        "ticket_id": "INC-REDIS-001",
        "service_name": "order-service",
        "incident_type": "redis_maxclients",
        "root_cause": "Redis maxclients exhausted by retry storm",
        "resolution": "Reduced retry amplification and raised maxclients after approval",
        "evidence": "connected_clients=9940 maxclients=10000 blocked_clients=37",
    },
    {
        "ticket_id": "INC-MYSQL-014",
        "service_name": "payment-service",
        "incident_type": "mysql_slow_query",
        "root_cause": "Slow SQL held MySQL connections and caused pool waiting",
        "resolution": "Captured digest, added index after approval, observed P95 recovery",
        "evidence": "slow_queries=18 active_connections=188/200 pool_waiting=6",
    },
]


def main() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    write_text_pdf(DOCS_DIR / "redis_postmortem.pdf", REDIS_PDF_LINES)
    (DOCS_DIR / "payment_wiki.html").write_text(PAYMENT_WIKI_HTML, encoding="utf-8")
    write_tickets_csv(DOCS_DIR / "tickets.csv")
    write_tickets_xlsx(DOCS_DIR / "tickets.xlsx")
    print(f"Generated multi-source RAG assets in {DOCS_DIR}")


def write_tickets_csv(path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(TICKET_ROWS[0].keys()))
        writer.writeheader()
        writer.writerows(TICKET_ROWS)


def write_tickets_xlsx(path: Path) -> None:
    workbook = Workbook()
    tickets = workbook.active
    tickets.title = "tickets"
    headers = list(TICKET_ROWS[0].keys())
    tickets.append(headers)
    for row in TICKET_ROWS:
        tickets.append([row[key] for key in headers])

    deploys = workbook.create_sheet("deploy_history")
    deploys.append(
        [
            "service_name",
            "version",
            "deployed_at",
            "change_summary",
            "risk_hint",
        ]
    )
    deploys.append(
        [
            "payment-service",
            "payment-api-2026.07.06-rc3",
            "2026-07-06T09:42:00Z",
            "Changed checkout order query path and ORM eager loading",
            "Correlates with slow query and pool_waiting=6",
        ]
    )
    deploys.append(
        [
            "order-service",
            "order-api-2026.07.06-rc1",
            "2026-07-06T09:10:00Z",
            "Raised Redis retry count in promotion lookup path",
            "Can amplify Redis maxclients pressure",
        ]
    )
    workbook.save(path)


def write_text_pdf(path: Path, lines: list[str]) -> None:
    """Write a tiny text PDF that pypdf can extract during indexing."""
    escaped_lines = [_pdf_text(line) for line in lines]
    text_commands = ["BT", "/F1 11 Tf", "50 780 Td", "14 TL"]
    for index, line in enumerate(escaped_lines):
        if index == 0:
            text_commands.append(f"({line}) Tj")
        else:
            text_commands.append(f"T* ({line}) Tj")
    text_commands.append("ET")
    stream = "\n".join(text_commands).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]

    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(obj)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(bytes(payload))


def _pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


if __name__ == "__main__":
    main()
