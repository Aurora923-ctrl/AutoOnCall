# AutoOnCall RAG Quality Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise the existing 12-case Runtime Full Judge baseline from `2/12` by aligning explicit question goals, frozen evidence, claim-level validation, and evaluation semantics without adding product features or case-specific answer templates.

**Architecture:** Build a deterministic `QuestionPlan`, map trusted retrieval chunks into one frozen generation evidence set, derive an `AnswerContract`, and run one bounded repair before a slot-aware extractive fallback. Preserve the public RAG API and existing retrieval backends while making Coverage, Prompt, Citation Guard, and evaluation consume the same evidence.

**Tech Stack:** Python 3.11, FastAPI, LangChain/LangGraph, Pydantic-compatible dataclasses, pytest/pytest-asyncio, Milvus, DashScope `qwen-max`, `text-embedding-v4`, RAGAS.

## Global Constraints

- Work directly on `main`; the user explicitly approved this exception.
- Preserve all pre-existing uncommitted changes; never reset, checkout, overwrite, or stage unrelated files.
- Do not add user-visible product features, new agents, models, databases, third-party dependencies, or public API fields.
- Do not increase `rag_top_k`, expand the knowledge base, or duplicate Golden Chain assets to obtain a passing score.
- Do not branch on evaluation `case_id`, full query strings, or expected answers.
- Keep existing streaming/non-streaming, refusal, citation, history, and response contracts compatible.
- Simple questions allow at most 3 claims; explicit multi-source or four-subgoal questions allow at most 5 claims.
- Coverage, Prompt, Citation Guard, and Judge contexts must use the same frozen evidence text and identities.
- Each production-code behavior change follows RED-GREEN-REFACTOR and must show the expected failing test before implementation.
- Task commits stage only paths named by that task; pre-existing edits inside a touched file must be preserved.
- Runtime acceptance may use real `qwen-max`, `text-embedding-v4`, Milvus rebuilds, and three Full Judge repeats; the user approved the external cost.

---

## File Structure

- Create `app/services/rag_question_plan.py`: explicit intent, entity, source-role, and claim-budget planning.
- Create `app/services/rag_evidence_plan.py`: source-role classification, subgoal binding, excerpt selection, and frozen evidence.
- Create `app/services/rag_answer_contract.py`: answer slots, deterministic violations, source contribution, and fallback selection.
- Modify `app/services/rag_answer_coverage.py`: compatibility facade backed by `QuestionPlan`/`AnswerContract` semantics.
- Modify `app/services/rag_generation_context.py`: format pre-frozen evidence without second excerpt selection.
- Modify `app/services/rag_generation_guard.py`: prepare one frozen evidence set and expose contract diagnostics.
- Modify `app/services/rag_answer_policy.py`: concise slot-driven prompt and slot-aware extractive fallback.
- Modify `app/services/rag_agent_service.py`: replace independent retry chains with one contract repair.
- Modify `scripts/eval/eval_ragas_cases.py`: precise citation/entity/source-contribution metrics and honest labels.
- Modify `eval/ragas_stage3_core_cases.yaml`: add explicit deterministic entity/source-role expectations without embedding answers.
- Add or extend focused tests under `tests/` for every module and integration boundary.

---

### Task 1: Explicit Question Planning

**Files:**
- Create: `app/services/rag_question_plan.py`
- Create: `tests/test_rag_question_plan.py`
- Modify: `app/services/rag_answer_coverage.py`
- Modify: `tests/test_rag_answer_coverage.py`

**Interfaces:**
- Consumes: raw user query string.
- Produces: `AnswerSubgoal`, `QuestionPlan`, `build_question_plan(query: str) -> QuestionPlan`, `entities_for_subgoal(plan: QuestionPlan, subgoal_id: str) -> tuple[str, ...]`.
- Compatibility: `build_answer_coverage_matrix()` remains callable and includes the old keys plus `question_plan`.

- [ ] **Step 1: Add failing intent and entity tests**

```python
from app.services.rag_question_plan import build_question_plan


def test_mysql_diagnosis_entities_do_not_imply_change_boundary() -> None:
    plan = build_question_plan(
        "payment-service 的 pool_waiting 和 active_connections 上升，如何排查慢查询？"
    )
    assert plan.domain == "mysql"
    assert set(plan.explicit_entities) >= {
        "payment-service",
        "pool_waiting",
        "active_connections",
        "慢查询",
        "EXPLAIN",
    }
    assert {item.intent for item in plan.subgoals} == {"evidence", "diagnosis"}
    assert all(not item.action_requested for item in plan.subgoals)
    assert plan.max_claims == 3


def test_redis_official_postmortem_question_requires_both_source_roles() -> None:
    plan = build_question_plan(
        "Redis connected_clients 接近 maxclients 时，如何结合官方限制和事故复盘判断？"
    )
    roles = {role for item in plan.subgoals for role in item.required_source_roles}
    assert {"official", "postmortem"}.issubset(roles)
    assert set(plan.explicit_entities) >= {
        "connected_clients",
        "maxclients",
        "effective_capacity",
        "blocked_clients",
    }
    assert plan.max_claims == 5


def test_explicit_rollback_request_adds_action_boundary() -> None:
    plan = build_question_plan("发布后 pool_waiting 上升，如何判断是否回滚？")
    assert any(item.intent == "action" and item.action_requested for item in plan.subgoals)
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
pytest tests/test_rag_question_plan.py -q
```

Expected: collection fails with `ModuleNotFoundError: app.services.rag_question_plan`.

- [ ] **Step 3: Implement immutable plan types and deterministic parsing**

Implement these exact public types:

```python
@dataclass(frozen=True, slots=True)
class AnswerSubgoal:
    id: str
    intent: str
    required_entities: tuple[str, ...]
    required_source_roles: tuple[str, ...]
    action_requested: bool = False
    temporal_boundary_required: bool = False


@dataclass(frozen=True, slots=True)
class QuestionPlan:
    query: str
    domain: str
    explicit_entities: tuple[str, ...]
    subgoals: tuple[AnswerSubgoal, ...]
    max_claims: int
```

Use normalized, table-driven domain definitions. For MySQL diagnosis, add `EXPLAIN` as a required diagnostic entity when the query explicitly asks how to investigate a slow query. For Redis official/postmortem capacity questions, add `effective_capacity` and `blocked_clients` as required diagnostic entities because they are required mechanism facets in the existing official source. Action markers are limited to explicit production actions: `重启`, `扩容`, `限流`, `回滚`, `删除`, `清理`, `截断`, `修改`, `调整`, `执行`, `变更`.

- [ ] **Step 4: Make answer coverage delegate to the plan**

Update `build_answer_coverage_matrix()` so its returned dictionary retains:

```python
{
    "query": query,
    "subgoals": [...],
    "required_count": int,
    "covered_count": int,
    "coverage_rate": float,
    "complete": bool,
    "uncovered_subgoals": [...],
}
```

and adds:

```python
"question_plan": {
    "domain": plan.domain,
    "explicit_entities": list(plan.explicit_entities),
    "max_claims": plan.max_claims,
}
```

Remove `maxclients`, `慢查询`, `pool_waiting`, and `active_connections` from implicit boundary markers. Preserve explicit boundary behavior.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
pytest tests/test_rag_question_plan.py tests/test_rag_answer_coverage.py -q
```

Expected: all tests pass; the prior test that asserted slow-query implied an action boundary is replaced with an assertion that it does not.

- [ ] **Step 6: Run adjacent retrieval tests**

Run:

```powershell
pytest tests/test_rag_retrieval_service.py tests/test_rag_retrieval_metadata.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit only Task 1 paths**

```powershell
git add app/services/rag_question_plan.py app/services/rag_answer_coverage.py tests/test_rag_question_plan.py tests/test_rag_answer_coverage.py
git commit -m "Add explicit RAG question planning"
```

---

### Task 2: Frozen Evidence Planning

**Files:**
- Create: `app/services/rag_evidence_plan.py`
- Create: `tests/test_rag_evidence_plan.py`
- Modify: `app/services/rag_generation_context.py`
- Modify: `tests/test_context_budget.py`
- Modify: `tests/test_rag_generation_guard.py`

**Interfaces:**
- Consumes: `QuestionPlan` from Task 1 and successful retrieval payload.
- Produces: `EvidenceBinding`, `FrozenGenerationEvidence`, `classify_source_role()`, `build_frozen_generation_evidence()`.
- Produces formatter: `format_frozen_generation_context(evidence: FrozenGenerationEvidence) -> str`.

- [ ] **Step 1: Add failing evidence-role and context-parity tests**

```python
from app.services.rag_evidence_plan import build_frozen_generation_evidence
from app.services.rag_generation_context import format_frozen_generation_context
from app.services.rag_question_plan import build_question_plan


def test_redis_frozen_evidence_covers_official_and_postmortem_roles() -> None:
    plan = build_question_plan(
        "Redis connected_clients 接近 maxclients 时，如何结合官方限制和事故复盘判断？"
    )
    payload = {
        "query": plan.query,
        "required_sources": ["official_redis_clients.md", "redis_postmortem.pdf"],
        "retrieval_results": [
            {
                "source_file": "official_redis_clients.md",
                "chunk_id": "official#capacity",
                "heading_path": "可执行查询与判据",
                "content": "effective_capacity=min(maxclients, os_fd_limit-reserved_fds); check blocked_clients",
            },
            {
                "source_file": "redis_postmortem.pdf",
                "chunk_id": "postmortem#root-cause",
                "heading_path": "Historical root cause",
                "content": "历史复盘：retry amplification increased client pressure.",
            },
        ],
    }
    frozen = build_frozen_generation_evidence(plan, payload)
    assert {item.source_role for item in frozen.bindings} >= {"official", "postmortem"}
    assert frozen.missing_subgoals == ()


def test_formatted_context_uses_exact_frozen_text_without_second_excerpt() -> None:
    plan = build_question_plan("Redis 容量是否安全，并结合历史复盘说明")
    middle_marker = "effective_capacity = maxclients - reserved_connections"
    payload = {
        "evidence": [
            {
                "id": 1,
                "source": "Redis官方文档.md",
                "content": ("前置说明。" * 200) + middle_marker + ("后置说明。" * 200),
            },
            {
                "id": 2,
                "source": "Redis连接耗尽复盘.pdf",
                "content": "历史复盘：retry amplification increased client pressure.",
            },
        ],
    }
    frozen = build_frozen_generation_evidence(plan, payload)
    rendered = format_frozen_generation_context(frozen)
    for item in frozen.items:
        assert str(item["content"]) in rendered
    assert middle_marker in rendered
```

The concrete fixture for the second test contains a long chunk with the required entity in the middle, proving that formatting does not call `select_generation_excerpt()` again.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
pytest tests/test_rag_evidence_plan.py -q
```

Expected: collection fails because `rag_evidence_plan` does not exist.

- [ ] **Step 3: Implement source-role classification and fair slot packing**

Implement:

```python
@dataclass(frozen=True, slots=True)
class EvidenceBinding:
    subgoal_id: str
    source_file: str
    chunk_id: str
    source_role: str
    matched_entities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FrozenGenerationEvidence:
    items: tuple[dict[str, Any], ...]
    bindings: tuple[EvidenceBinding, ...]
    missing_subgoals: tuple[str, ...]
    missing_entities: tuple[str, ...]
```

Role rules use existing metadata and source form: PDF/postmortem markers map to `postmortem`, table/tickets map to `ticket`, official snapshot metadata or `official_` maps to `official`, ordinary operational documents map to `runbook`. Never classify static knowledge as `current`.

Select excerpts once using the existing `select_generation_excerpt()` helper. Reserve one matching evidence item for every required source role, then one for every uncovered subgoal/entity, then fill remaining character budget by rerank order. Return an empty evidence set when an explicitly required source cannot fit.

- [ ] **Step 4: Add a formatter that cannot reselect excerpts**

Implement `format_frozen_generation_context()` by enumerating `FrozenGenerationEvidence.items` and rendering the already assigned `citation_index`, `source_file`, `chunk_id`, and content. Update `build_generation_context()` to use this formatter when the retrieval payload carries `_frozen_generation_evidence`.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
pytest tests/test_rag_evidence_plan.py tests/test_context_budget.py tests/test_rag_generation_guard.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Run generation-context regressions**

Run:

```powershell
pytest tests/test_rag_agent_citations.py tests/test_chat_rag_api.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit only Task 2 paths**

```powershell
git add app/services/rag_evidence_plan.py app/services/rag_generation_context.py tests/test_rag_evidence_plan.py tests/test_context_budget.py tests/test_rag_generation_guard.py
git commit -m "Freeze RAG generation evidence"
```

---

### Task 3: Claim-Level Answer Contract

**Files:**
- Create: `app/services/rag_answer_contract.py`
- Create: `tests/test_rag_answer_contract.py`
- Modify: `app/services/rag_generation_guard.py`
- Modify: `tests/test_rag_generation_guard.py`

**Interfaces:**
- Consumes: `QuestionPlan`, `FrozenGenerationEvidence`, final answer text, server-issued citations.
- Produces: `AnswerSlot`, `AnswerContract`, `ContractViolation`, `build_answer_contract()`, `validate_answer_contract()`, `contract_repair_instructions()`.
- Extends `GenerationPreparation` with frozen evidence and contract without changing public API output.

- [ ] **Step 1: Add failing contract validation tests**

```python
def test_contract_reports_missing_mysql_entities() -> None:
    contract = mysql_contract()
    violations = validate_answer_contract(
        "- 检查 slow_queries 和 connection hold time。[证据 1]",
        contract,
        citations(),
    )
    assert {item.code for item in violations} >= {
        "missing_entity:pool_waiting",
        "missing_entity:active_connections",
        "missing_entity:EXPLAIN",
    }


def test_contract_rejects_unrequested_change_template() -> None:
    contract = mysql_diagnosis_only_contract()
    violations = validate_answer_contract(
        "- 变更计划包含 approver、canary、观察时长和 rollback。[证据 2]",
        contract,
        citations(),
    )
    assert any(item.code == "unrequested_change_template" for item in violations)


def test_contract_requires_postmortem_claim_contribution() -> None:
    contract = redis_multi_source_contract()
    violations = validate_answer_contract(
        "- 检查 effective_capacity 和 blocked_clients。[证据 1]",
        contract,
        citations(),
    )
    assert any(item.code == "missing_source_role:postmortem" for item in violations)


def test_contract_rejects_two_citations_on_one_claim() -> None:
    violations = validate_answer_contract(
        "- 历史工单不能替代当前证据。[证据 1][证据 2]",
        ticket_contract(),
        citations(),
    )
    assert any(item.code == "multiple_citations_in_claim" for item in violations)
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
pytest tests/test_rag_answer_contract.py -q
```

Expected: collection fails because `rag_answer_contract` does not exist.

- [ ] **Step 3: Implement contract construction and violations**

Implement exact public dataclasses from the design. `ContractViolation.code` uses stable machine-readable values. Entity checks are case-insensitive and alias-aware but require concrete entity matches, not generic CJK n-gram overlap. Each substantive line must resolve to exactly one citation; concrete evidence-gap lines carry no citation.

Required-source contribution resolves citation index to the frozen evidence binding and counts only a claim using the correct role. Historical claims from `postmortem` or `ticket` require one of: `历史`, `复盘`, `工单`, `部署记录`, `historical`, `retrospective`.

- [ ] **Step 4: Integrate contract into generation preparation**

Extend `GenerationPreparation` with:

```python
frozen_evidence: FrozenGenerationEvidence | None = None
answer_contract: AnswerContract | None = None
```

`prepare_grounded_generation()` builds the plan once, builds frozen evidence once, builds citations from frozen items, and attaches the contract to the internal generation payload under `_answer_contract`. Existing refusal behavior remains unchanged.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
pytest tests/test_rag_answer_contract.py tests/test_rag_generation_guard.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Run citation guard regressions**

Run:

```powershell
pytest tests/test_rag_agent_citations.py tests/test_rag_boundaries.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit only Task 3 paths**

```powershell
git add app/services/rag_answer_contract.py app/services/rag_generation_guard.py tests/test_rag_answer_contract.py tests/test_rag_generation_guard.py
git commit -m "Validate grounded answers by contract"
```

---

### Task 4: Slot-Driven Prompt and One Repair Path

**Files:**
- Modify: `app/services/rag_answer_policy.py`
- Modify: `app/services/rag_agent_service.py`
- Modify: `tests/test_rag_agent_citations.py`
- Modify: `tests/test_chat_rag_api.py`
- Create: `tests/test_rag_contract_repair.py`

**Interfaces:**
- Consumes: `_answer_contract` and `_frozen_generation_evidence` from Task 3.
- Produces: concise grounded prompt, one contract repair call, and slot-aware extractive fallback.
- Public `query_with_retrieval()` response remains unchanged.

- [ ] **Step 1: Add failing orchestration tests**

Implement three async tests with the existing `rag_agent_service` fixture and monkeypatch seam used by
`tests/test_rag_agent_citations.py`:

1. `test_contract_failure_triggers_only_one_targeted_repair`: return first
   `"- 检查 slow_queries。[证据 1]"`, then
   `"- 检查 pool_waiting、active_connections 和慢 SQL。[证据 1]\n- 对规范化 digest 执行只读 EXPLAIN。[证据 2]"`.
   Record both prompts and assert exactly two model calls, the second prompt contains
   `missing_entity:EXPLAIN`, and `result["no_answer"] is False`.
2. `test_valid_first_answer_does_not_retry`: return a first answer that contains every MySQL slot and
   valid citations; assert exactly one model call.
3. `test_failed_repair_uses_slot_aware_fallback`: return the same invalid Redis answer twice; assert
   exactly two model calls, the final answer contains `effective_capacity`, `历史复盘`, and at least two
   `[证据 N]` citations drawn from the frozen evidence.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
pytest tests/test_rag_contract_repair.py -q
```

Expected: tests fail because the current orchestration uses independent required-source, coverage, and citation retries.

- [ ] **Step 3: Replace the duplicated prompt contract**

Keep the system prompt limited to grounding, citation, static/history/current boundaries, and output shape. Build the user prompt from exact answer slots:

```text
答案槽位：
- diagnosis.mysql.metrics: 必须出现 pool_waiting、active_connections、慢查询；允许证据 [1]
- diagnosis.mysql.explain: 必须出现 EXPLAIN；允许证据 [2]
```

Do not append approval/dry-run/rollback instructions when no action slot exists. Use `contract.max_claims` instead of a global three-line limit.

- [ ] **Step 4: Collapse retries into one Contract repair**

After `finalize_grounded_answer()`, call `validate_answer_contract()`. If violations exist, build one repair prompt containing only violation codes, affected slots, allowed evidence numbers, and the original frozen context. Invoke the model once. Accept the repair only when citation validation and contract validation both pass.

Delete the independent required-source retry and answer-coverage retry blocks. Preserve provider retry behavior for transient network failures; that is not a semantic generation retry.

- [ ] **Step 5: Make extractive fallback slot-aware**

Extend `build_extractive_grounded_answer()` to accept `AnswerContract` and choose one supported sentence per missing slot/source role. Never put two citations on one line. Emit `当前证据不足：缺少 <entity/subgoal>` only when the slot has no allowable sentence.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```powershell
pytest tests/test_rag_contract_repair.py tests/test_rag_agent_citations.py tests/test_chat_rag_api.py -q
```

Expected: all tests pass and semantic generation attempts are bounded to two.

- [ ] **Step 7: Run stream/non-stream boundary tests**

Run:

```powershell
pytest tests/test_rag_stream_boundaries.py tests/test_rag_runtime_observability.py -q
```

Expected: all tests pass with unchanged public events and response fields.

- [ ] **Step 8: Commit only Task 4 paths**

```powershell
git add app/services/rag_answer_policy.py app/services/rag_agent_service.py tests/test_rag_agent_citations.py tests/test_chat_rag_api.py tests/test_rag_contract_repair.py
git commit -m "Drive RAG generation from answer contracts"
```

---

### Task 5: Honest Evaluation Metrics and Golden-Chain Regressions

**Files:**
- Modify: `scripts/eval/eval_ragas_cases.py`
- Modify: `eval/ragas_stage3_core_cases.yaml`
- Modify: `tests/test_ragas_eval_cases.py`
- Modify: `tests/test_rag_eval_cases.py`
- Modify: `tests/test_rag_scorecard.py`

**Interfaces:**
- Consumes: final answer, citations, frozen evaluation contexts, case expectations.
- Produces metrics: `citation_membership_score`, `required_source_contribution_score`, `claim_support_score`, `entity_coverage_score`, precise `temporal_boundary_hit`.
- Compatibility: retain existing citation metric fields while correcting report descriptions.

- [ ] **Step 1: Add failing metric semantics tests**

```python
def test_required_source_contribution_fails_when_redis_postmortem_is_uncited() -> None:
    sample = redis_sample(
        answer="- 检查 effective_capacity 和 blocked_clients。[证据 1]",
        citations=[official_citation()],
        retrieved_ids=["official_redis_clients.md", "redis_postmortem.pdf"],
    )
    metrics = business_metric_scores(sample)
    assert metrics["citation_membership_score"] == 1.0
    assert metrics["required_source_contribution_score"] == 0.5


def test_temporal_boundary_requires_historical_label_on_ticket_claim() -> None:
    sample = ticket_sample("- INC-REDIS-009 证明当前根因是 maxclients。[证据 1]")
    assert business_metric_scores(sample)["temporal_boundary_hit"] == 0.0


def test_entity_coverage_requires_all_explicit_mysql_entities() -> None:
    sample = mysql_sample("- 检查 slow_queries。[证据 1]")
    assert business_metric_scores(sample)["entity_coverage_score"] < 1.0
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
pytest tests/test_ragas_eval_cases.py -q
```

Expected: the new metric keys are missing or current permissive boundary logic returns the wrong value.

- [ ] **Step 3: Implement precise deterministic metrics**

`citation_membership_score` measures cited identity membership in frozen retrieved IDs. `required_source_contribution_score` measures required source files actually cited by claims. `claim_support_score` reuses contract/entity binding instead of generic token overlap. `entity_coverage_score` uses case `required_answer_entities`. `temporal_boundary_hit` checks every claim using a ticket/postmortem citation for explicit historical language and rejects current-fact wording.

Retain `citation_support_score` as a compatibility alias for membership and update report text to say “citation membership,” not semantic support.

- [ ] **Step 4: Add explicit case expectations without answer templates**

Add to the YAML cases:

```yaml
required_answer_entities:
  - pool_waiting
  - active_connections
  - 慢查询
  - EXPLAIN
required_source_roles:
  - runbook
```

for MySQL, and:

```yaml
required_answer_entities:
  - connected_clients
  - maxclients
  - effective_capacity
  - blocked_clients
required_source_roles:
  - official
  - postmortem
```

for Redis. Add equivalent explicit entities to the remaining positive cases using terms already present in their query/reference rubric. Do not add generated answer sentences.

- [ ] **Step 5: Add paraphrase and negative regression cases in tests**

Tests call planning/contract code with paraphrased Redis/MySQL questions and a diagnosis-only query. Assert no `case_id` lookup, the same required entities, and no unrequested action slot.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```powershell
pytest tests/test_ragas_eval_cases.py tests/test_rag_eval_cases.py tests/test_rag_scorecard.py -q
```

Expected: all tests pass with new metrics and compatibility fields.

- [ ] **Step 7: Run the full quick suite**

Run:

```powershell
make test-quick
```

If GNU Make is unavailable, run:

```powershell
pytest -q
```

Expected: all repository tests pass.

- [ ] **Step 8: Commit only Task 5 paths**

```powershell
git add scripts/eval/eval_ragas_cases.py eval/ragas_stage3_core_cases.yaml tests/test_ragas_eval_cases.py tests/test_rag_eval_cases.py tests/test_rag_scorecard.py
git commit -m "Measure RAG answer contracts precisely"
```

---

### Task 6: Verification, Runtime Acceptance, and Evidence Update

**Files:**
- Modify only if results require wording/provenance updates: `docs/AutoOnCall当前问题与后续改进计划.md`
- Create runtime artifacts under ignored `logs/`; never commit them unless repository policy explicitly tracks the selected summary.
- Modify focused code/tests only through a new RED-GREEN cycle if verification finds a defect.

**Interfaces:**
- Consumes: all prior task commits, local Milvus, DashScope credentials, existing evaluation scripts.
- Produces: clean test evidence, 37-case ID Smoke artifact, three 12-case Runtime Full Judge artifacts, before/after comparison, final reviewer verdict.

- [ ] **Step 1: Run formatting, lint, and type checks**

Run:

```powershell
ruff check app tests scripts
black --check app tests scripts
isort --check-only app tests scripts
mypy app
```

Expected: zero errors. If the repository wrappers differ, use `make lint` and `make type-check` and record the exact commands/output.

- [ ] **Step 2: Run the complete test suite**

Run:

```powershell
pytest -q
```

Expected: all collected tests pass. Record pass count, duration, warnings, and any intentionally skipped environment tests.

- [ ] **Step 3: Rebuild and verify the RAG index**

Run the repository's documented maintenance commands:

```powershell
.\.venv\Scripts\python.exe scripts/maintenance/rebuild_rag_index.py --confirm-drop --report logs/rag_index_rebuild_contract_20260722.json
.\.venv\Scripts\python.exe scripts/eval/rag_index_identity.py
```

Expected: Milvus collection is loaded, embedding dimension is `1024`, knowledge asset hash and index identity agree, and runtime retrieval does not silently fall back to lexical.

- [ ] **Step 4: Run 37-case Runtime ID Smoke**

Run:

```powershell
.\.venv\Scripts\python.exe scripts/eval/eval_ragas_cases.py --cases eval/rag_cases.yaml --mode runtime --answer-source runtime --metrics-profile id-smoke --repeat-count 1 --summary-json logs/ragas_runtime_id_smoke_contract_20260722.json --summary-md logs/ragas_runtime_id_smoke_contract_20260722.md --failed-cases-json logs/ragas_runtime_id_smoke_contract_20260722_failed.json
```

Expected: `37/37`, citation existence `100%`, no invalid input, and the artifact records runtime Milvus retrieval.

- [ ] **Step 5: Run three independent 12-case Runtime Full Judge evaluations**

Run these three separate commands so each repeat remains independently reviewable:

```powershell
.\.venv\Scripts\python.exe scripts/eval/eval_ragas_cases.py --cases eval/ragas_stage3_core_cases.yaml --mode runtime --answer-source runtime --metrics-profile full --repeat-count 1 --summary-json logs/ragas_full_runtime_contract_r1_20260722.json --summary-md logs/ragas_full_runtime_contract_r1_20260722.md --failed-cases-json logs/ragas_full_runtime_contract_r1_20260722_failed.json
.\.venv\Scripts\python.exe scripts/eval/eval_ragas_cases.py --cases eval/ragas_stage3_core_cases.yaml --mode runtime --answer-source runtime --metrics-profile full --repeat-count 1 --summary-json logs/ragas_full_runtime_contract_r2_20260722.json --summary-md logs/ragas_full_runtime_contract_r2_20260722.md --failed-cases-json logs/ragas_full_runtime_contract_r2_20260722_failed.json
.\.venv\Scripts\python.exe scripts/eval/eval_ragas_cases.py --cases eval/ragas_stage3_core_cases.yaml --mode runtime --answer-source runtime --metrics-profile full --repeat-count 1 --summary-json logs/ragas_full_runtime_contract_r3_20260722.json --summary-md logs/ragas_full_runtime_contract_r3_20260722.md --failed-cases-json logs/ragas_full_runtime_contract_r3_20260722_failed.json
```

Expected for every repeat:

- cases `12/12`;
- Faithfulness `>= 0.85`;
- Response Relevancy `>= 0.80`;
- Answer Completeness `>= 0.95`;
- citation existence `100%`;
- required source contribution `100%`;
- refusal boundary `100%`;
- Redis/MySQL pass.

- [ ] **Step 6: Independently review Judge disagreements**

For any case below threshold, provide the final answer, frozen evidence, contract, and metric diagnostics to a fresh review subagent. Classify the failure as product defect, contract defect, retrieval defect, or Judge calibration issue. Do not lower thresholds or add query-specific markers without a failing paraphrase/generalization test.

- [ ] **Step 7: Update the problem document with verified facts**

Only after successful artifacts exist, update the document with before/after metrics, exact artifact paths, evidence level, limitations, code commit, Prompt hash, knowledge hash, and remaining risks. Do not call a dirty-worktree or offline-fixture artifact an official production baseline.

- [ ] **Step 8: Run final full-branch review**

Dispatch a fresh high-capability reviewer with the approved design, this plan, full diff package, task reports, test output, and three runtime artifacts. Required verdicts:

- spec compliance;
- code quality;
- no evaluation hardcoding;
- test credibility;
- metric stability;
- capability-boundary honesty;
- autumn-recruiting competitiveness for the existing feature set.

- [ ] **Step 9: Commit only verified documentation changes**

```powershell
git add docs/AutoOnCall当前问题与后续改进计划.md
git commit -m "Record verified RAG quality improvements"
```

Do not stage ignored runtime logs or unrelated files.
