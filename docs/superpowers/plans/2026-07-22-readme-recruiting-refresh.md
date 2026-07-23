# AutoOnCall Recruiting README Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the root README as a complete, recruiting-oriented explanation of AutoOnCall's business value, architecture, technical depth, evidence, usage, and boundaries.

**Architecture:** Keep `README.md` as the repository-wide entrypoint and preserve deep technical material through links to existing topic documents. Reorder the narrative from business value to system design, implementation evidence, reproducibility, and production boundaries while retaining accurate commands, APIs, metrics, and paths.

**Tech Stack:** Markdown, Mermaid, Python 3.11, FastAPI, LangGraph, DashScope, Milvus, SQLite/MySQL, Prometheus, Loki, Kubernetes, Redis, pytest, Ruff, mypy, ESLint, Docker Compose.

## Global Constraints

- The primary reader is a recruiting interviewer; developers are the secondary reader.
- Keep a complete README with roughly the current level of operational detail.
- Treat the 2026-07-22 working tree as the fact baseline.
- Clearly distinguish candidate, official, offline, live adapter, sandbox, mock/fallback, and production evidence.
- Do not modify application behavior, dependencies, deployment configuration, or evaluation assets.
- Do not stage, revert, or overwrite unrelated working-tree changes.

---

### Task 1: Rebuild the recruiting narrative and architecture sections

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: current repository implementation, `pyproject.toml`, `Makefile`, `app/main.py`, and the approved design spec.
- Produces: README opening sections covering project positioning, business problems, differentiators, architecture, business flow, technical challenges, and categorized stack.

- [ ] **Step 1: Replace the opening information hierarchy**

Write an opening that answers what AutoOnCall is, which OnCall problems it solves, and why it is more than a generic RAG chatbot.

- [ ] **Step 2: Consolidate core highlights**

Describe Plan—Execute—Replan, trusted RAG, Evidence-driven RCA, approvals, safe change execution, replay, observability, and layered evaluation with explicit implementation boundaries.

- [ ] **Step 3: Rework architecture and business flow**

Keep one valid Mermaid architecture diagram and one compact end-to-end text flow. Explain alert ingestion, planning, tool evidence, replanning, reporting, approval, and human escalation.

- [ ] **Step 4: Add technical challenges and categorized technology stack**

For each major design choice, state the problem, implementation, value, and boundary. Group technologies by API, Agent, RAG, data, integrations, observability, engineering quality, and UI responsibilities.

- [ ] **Step 5: Review the first half**

Run: `rg -n '^#{1,3} ' README.md`

Expected: project positioning, value, architecture, technical challenges, and stack appear before operational instructions and no duplicate headings exist.

### Task 2: Preserve and reorganize implementation evidence and operating guidance

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1 section order and existing README facts.
- Produces: complete core flows, scorecard, demo path, startup, API, configuration, quality gates, layout, boundaries, and document navigation.

- [ ] **Step 1: Preserve the four core implementation flows**

Document alert ingestion, trusted RAG, AIOps diagnosis, and safe changes with their endpoints, data flow, fallback behavior, and safety boundaries.

- [ ] **Step 2: Move quantitative evidence after the design explanation**

Retain the current verified values and link to their source documents. Explain that dirty-worktree results are candidate evidence and do not establish production accuracy.

- [ ] **Step 3: Consolidate interview and demonstration guidance**

Keep the five-minute Redis/MySQL live-adapter path, identify K8s as offline regression evidence, and remove duplicated checklists or table headers.

- [ ] **Step 4: Preserve reproducibility material**

Retain accurate installation, Docker, upload, startup, API, environment, validation, directory, security, and deep-documentation instructions.

- [ ] **Step 5: Check factual anchors**

Run: `rg -n '1\.2\.1|Python 3\.11|make verify|make live-eval|make interview-up|make sandbox-verify|/api/aiops|/api/chat|AIOPS_MOCK_FALLBACK_ENABLED' README.md`

Expected: every anchor appears in the correct context and is not contradicted elsewhere.

### Task 3: Validate the finished README

**Files:**
- Verify: `README.md`

**Interfaces:**
- Consumes: Tasks 1 and 2 output.
- Produces: a structurally valid, fact-checked README diff ready for user review.

- [ ] **Step 1: Check Markdown whitespace and conflict markers**

Run: `git diff --check -- README.md`

Expected: no output and exit code 0.

- [ ] **Step 2: Check headings and fenced blocks**

Run a read-only PowerShell validation that asserts exactly one H1, no duplicate adjacent table headers, no conflict markers, and an even number of triple-backtick fence lines.

Expected: `README structure OK`.

- [ ] **Step 3: Check local Markdown links**

Run a read-only PowerShell validation that extracts relative Markdown link targets, URL-decodes them, strips anchors, and confirms each referenced local path exists.

Expected: `README local links OK`.

- [ ] **Step 4: Review scope and diff**

Run: `git diff --stat -- README.md` and `git diff -- README.md`

Expected: only the intended README rewrite is shown, with no unsupported capability claims, placeholders, or unrelated edits.

- [ ] **Step 5: Report verification evidence**

Summarize the new narrative, exact validations run, and any evidence limitations. Do not claim project tests passed because this task changes documentation only.
