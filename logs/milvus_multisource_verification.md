# Milvus Multi-Source RAG Verification

## Summary

- Status: `passed`
- Collection: `autooncall_interview_multisource`
- Inserted chunks: `18`
- Probe pass rate: `6/6`
- Scope: Milvus storage/provenance verification for multi-source RAG assets; deterministic local vectors are used to avoid cloud embedding dependency.

## Source Coverage

| Source | Chunks |
| --- | ---: |
| `mysql_slow_query_postmortem.pdf` | 1 |
| `payment_wiki.html` | 2 |
| `redis_capacity_wiki.html` | 2 |
| `redis_postmortem.pdf` | 1 |
| `tickets.csv` | 4 |
| `tickets.xlsx` | 8 |

## Probe Results

| Probe | Expected source | Status | Top sources |
| --- | --- | --- | --- |
| `redis_pdf_postmortem` | `redis_postmortem.pdf` | `PASS` | tickets.csv, tickets.xlsx, redis_capacity_wiki.html |
| `mysql_pdf_postmortem` | `mysql_slow_query_postmortem.pdf` | `PASS` | payment_wiki.html, tickets.csv, tickets.xlsx |
| `redis_html_wiki` | `redis_capacity_wiki.html` | `PASS` | redis_capacity_wiki.html, redis_capacity_wiki.html, redis_postmortem.pdf |
| `payment_html_wiki` | `payment_wiki.html` | `PASS` | payment_wiki.html, tickets.xlsx, tickets.xlsx |
| `ticket_csv_history` | `tickets.csv` | `PASS` | tickets.csv, tickets.xlsx, tickets.csv |
| `deploy_xlsx_history` | `tickets.xlsx` | `PASS` | tickets.xlsx, tickets.csv, tickets.xlsx |
