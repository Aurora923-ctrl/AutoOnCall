# AutoOnCall RAG 质量契约改进设计

## 1. 背景与目标

AutoOnCall 已具备混合检索、Milvus、引用、拒答、知识治理、Runtime Full Judge、
Redis/MySQL Golden Chain 等现有能力。本次改造不增加新的业务功能，只修复现有 RAG
回答链路中问题规划、证据选择、生成约束、引用校验和评测口径不一致的问题。

当前基线来自 `logs/ragas_full_runtime_core_20260722_rerun.json`：核心案例通过率为
`2/12`，Faithfulness 为 `0.8367`，Response Relevancy 为 `0.4314`。检索 ID、引用存在、
拒答边界已经基本稳定，因此本轮不扩大知识库，不增加 Top-K，不增加模型或外部依赖。

本轮目标：

1. 让回答只覆盖用户明确询问的子问题和实体。
2. 让 Coverage、Prompt、Citation Guard 和 Judge 使用同一份冻结证据。
3. 让必要来源真正贡献答案 claim，而不只是出现在检索结果中。
4. 区分引用存在、引用成员关系、来源完整性和 claim 支持。
5. 提升现有 12 个核心案例指标，同时用改写和负例防止评测过拟合。
6. 保持现有 API、流式/非流式行为、拒答边界和 Golden Chain 不变。

## 2. 范围与非目标

### 2.1 范围

- 结构化解析用户显式问题目标。
- 按子目标和来源职责选择生成证据。
- 冻结一次最终生成证据。
- 建立答案槽位和确定性校验。
- 收敛当前多轮、相互覆盖的生成修复流程。
- 修正评测指标语义和诊断分类。
- 强化 Redis/MySQL 现有 Golden Chain 的回归覆盖。
- 执行真实 Milvus、`qwen-max` 和 `text-embedding-v4` 验收。

### 2.2 非目标

- 不新增时序分析、根因假设竞争、信息增益调度等业务能力。
- 不增加新的 Agent、模型、数据库或第三方依赖。
- 不重写现有检索后端和向量索引体系。
- 不为 12 个案例硬编码完整答案或按 `case_id` 分支。
- 不用扩大知识库、提高 Top-K 或复制文档内容掩盖生成问题。
- 不声称生产准确率、生产 MTTD/MTTR 或正式官方基线。

## 3. 方案选择

采用“质量契约优先的中等重构”。

未采用 Prompt-only 方案，因为当前 system prompt 与 grounded question 已包含大量重复、
互相竞争的规则，继续追加指令不能解决证据裁剪和校验错位。未采用固定案例模板，因为它会
直接造成题库过拟合，无法通过问题改写和面试现场追问。

## 4. 总体架构

```text
用户问题
  -> QuestionPlan
  -> 现有混合检索
  -> EvidencePlan
  -> FrozenGenerationEvidence
  -> AnswerContract 驱动生成
  -> ContractValidation
       - 实体覆盖
       - 子目标覆盖
       - 单 claim 单引用
       - 必要来源贡献
       - 历史/当前边界
       - 无关模板检测
  -> 一次定向修复
  -> 按槽位抽取的确定性 fallback 或明确局部缺口
  -> 现有 API 响应
```

外部调用方继续使用 `RagAgentService.query_with_retrieval()` 和现有响应字段。新增结构只作为
服务内部数据，不改变前端和公共 API 契约。

## 5. 组件设计

### 5.1 `rag_question_plan.py`

职责：将用户问题转换为稳定、可测试的 `QuestionPlan`。

建议接口：

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


def build_question_plan(query: str) -> QuestionPlan:
    ...
```

规则要求：

- 只从用户显式措辞创建子目标。
- `pool_waiting`、`active_connections`、`慢查询`、`maxclients` 是诊断实体，不自动创建处置子目标。
- 只有明确询问重启、扩容、限流、回滚、删除、清理、配置变更等动作时，才创建处置边界子目标。
- “官方”创建机制来源职责；“复盘”创建历史来源职责；“工单/部署历史”创建历史记录职责。
- 简单问题最多 3 个 claim；多来源或 4 个以上显式子目标时最多 5 个 claim。
- 规则以领域和意图为单位，不使用评测 `case_id` 或完整问题文本。

### 5.2 `rag_evidence_plan.py`

职责：将已通过信任门禁的检索结果映射到问题子目标，并一次性生成最终证据。

建议接口：

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


def build_frozen_generation_evidence(
    plan: QuestionPlan,
    retrieval_payload: dict[str, Any],
) -> FrozenGenerationEvidence:
    ...
```

来源职责：

- `official`：产品机制、限制、参数含义和官方命令。
- `runbook`：调查步骤、判断路径和受控处置边界。
- `postmortem`：历史现象、历史根因、排除过程和历史验证。
- `ticket`：历史版本、发布、审批和处置记录。
- `current`：当前或 incident-window 工具事实；静态知识库不得伪装成该角色。

选择原则：

- 优先覆盖显式实体和子目标，而不是只按总体 rerank score 取前三条。
- 必要来源先各保留一个真正匹配对应职责的 chunk。
- excerpt selection 在本阶段完成且只执行一次。
- Coverage、Prompt、Citation Guard、评测上下文都复用 `items`，不得再次截取。
- 保留当前总字符预算；预算不足时优先保留每个必要来源和显式子目标的最小证据。
- 无法容纳必要来源时失败关闭，不静默丢弃来源。

### 5.3 `rag_answer_contract.py`

职责：从 `QuestionPlan` 和冻结证据建立答案槽位，并校验生成结果。

建议接口：

```python
@dataclass(frozen=True, slots=True)
class AnswerSlot:
    subgoal_id: str
    required_entities: tuple[str, ...]
    allowed_citation_indices: tuple[int, ...]
    required_source_roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerContract:
    slots: tuple[AnswerSlot, ...]
    max_claims: int


@dataclass(frozen=True, slots=True)
class ContractViolation:
    code: str
    subgoal_id: str = ""
    detail: str = ""


def validate_answer_contract(
    answer: str,
    contract: AnswerContract,
    citations: list[dict[str, Any]],
) -> tuple[ContractViolation, ...]:
    ...
```

校验要求：

- 每个非缺口 claim 只有一个 `[证据 N]`。
- 每个显式子目标至少有一个 claim 或一个具体局部缺口。
- 核心实体必须出现在对应 claim 中，不能只存在于上下文。
- 多来源问题的必要来源职责必须真正贡献 claim。
- postmortem/ticket claim 必须显式包含“历史、复盘、工单、部署记录”等边界语言。
- 未询问处置时，通用审批、执行人、canary、观察时长不能成为独立 claim。
- “当前证据不足”必须指出缺少什么证据，不接受无对象的泛化句式。

### 5.4 兼容层

`rag_answer_coverage.py` 保留现有公共函数，但内部委托新 `QuestionPlan` 和
`AnswerContract`。现有调用方和测试可以渐进迁移，不进行无关的大规模文件重组。

`rag_generation_context.py` 增加直接格式化冻结证据的入口，旧入口保持兼容。生成阶段不得
重新执行 excerpt selection。

## 6. 生成与失败处理

### 6.1 首次生成

Prompt 只包含：

- 最终冻结证据；
- 用户原始问题；
- 答案槽位；
- 简短的引用和历史/当前边界规则。

删除重复的 20 条大段规则和 system/user prompt 的同义约束。输出仍为用户可读要点，不向
外部暴露内部 `QuestionPlan`。

### 6.2 一次定向修复

Contract 校验失败时，将明确 violation 传给模型，例如：

- `missing_entity: EXPLAIN`
- `missing_source_role: postmortem`
- `multiple_citations_in_claim`
- `unrequested_change_template`
- `unspecified_evidence_gap`

修复请求只能补充或替换违规槽位，不得重新扩写无关内容。整条生成链最多进行一次 Contract
修复，避免当前多轮来源、覆盖和引用重试互相覆盖。

### 6.3 确定性 fallback

修复后仍不合格时，按答案槽位从其允许证据中抽取最相关的完整句子，并附对应引用。多来源
问题必须为每个必要来源分别生成 claim。找不到支持句时只输出具体缺口，不使用通用审批模板
填充。

### 6.4 最终状态

- 检索无可信来源：保持现有拒答行为。
- 必要来源检索缺失：拒绝完整强答并列出缺失来源。
- 部分子目标缺证据：回答有证据部分并列出具体局部缺口。
- 引用身份无效：保持 fail-closed。
- Contract 合格：返回现有 `answer_with_citations`。

## 7. 评测语义修正

现有指标拆分为：

1. `citation_existence_hit`：答案包含可解析引用。
2. `citation_membership_score`：引用属于冻结生成证据。
3. `required_source_contribution_score`：必要来源是否实际贡献 claim。
4. `claim_support_score`：claim 与唯一绑定证据是否满足确定性支持条件。
5. `entity_coverage_score`：问题显式实体是否进入对应答案槽位。
6. `temporal_boundary_hit`：历史来源是否被明确标为历史，且未冒充当前事实。

保留旧字段作为兼容输出，但报告文案不得再把“引用 ID 属于检索集合”表述为完整语义支持。

Response Relevancy 继续保留为 Judge 指标，但在阈值调整前必须完成：

- CPU、内存、磁盘等争议案例的独立人工/子 Agent 复核；
- Judge 与人工结论对照；
- 记录阈值调整理由，禁止只为通过当前 12 个案例降低门槛。

## 8. 测试设计

### 8.1 单元测试

- QuestionPlan 的领域、意图、实体和来源职责解析。
- 诊断实体不误触发处置边界。
- 多来源问题动态获得最多 5 个 claim。
- EvidencePlan 覆盖每个显式实体和必要来源。
- 冻结证据内容与 Prompt 内容完全一致。
- Contract 检出缺实体、多引用、缺来源贡献、历史越界和通用模板。
- 定向修复只允许一次。
- fallback 按槽位和来源生成，不混合多来源 claim。

### 8.2 回归测试

- 保留现有 12 个核心案例和 37 个 ID Smoke 案例。
- Redis 同义改写必须保留 `effective_capacity`、`blocked_clients`、连接所有者和复盘职责。
- MySQL 同义改写必须保留 `pool_waiting`、`active_connections`、慢 SQL 和 `EXPLAIN`。
- 增加缺少必要来源、只有历史材料、未询问处置等负例。
- 验证流式/非流式、API 返回、拒答和引用不回退。

### 8.3 TDD 约束

每项行为修改必须先增加失败测试并观察到预期失败，再进行最小实现。每个任务完成后运行其
定向测试和相关回归测试；最终运行完整测试套件。

## 9. 真实验收

用户已批准真实 `qwen-max`、Embedding、Milvus 重建和三次 Full Judge 调用费用。

固定执行顺序：

1. 运行相关单元测试和快速测试。
2. 运行完整仓库测试。
3. 重建 Milvus 索引并验证知识资产 hash 与 index identity。
4. 运行 37 案例 Runtime ID Smoke。
5. 运行 12 案例 Runtime Full Judge，固定配置连续 3 次。
6. 保存代码提交、工作树状态、Prompt hash、知识库 hash、逐案例答案和失败集合。
7. 对最终答案执行独立子 Agent 审查，并与 Judge 结果比较。

验收目标：

- Runtime ID Smoke：`37/37`。
- 三次 Runtime Full Judge 的核心案例均为 `12/12`。
- Faithfulness：每次 `>= 0.85`。
- Response Relevancy：每次 `>= 0.80`；若与独立审查冲突，必须先校准评测，不能刷关键词。
- Answer Completeness：每次 `>= 0.95`。
- Citation existence：`100%`。
- Required source contribution：`100%`。
- Refusal boundary：`100%`。
- Redis/MySQL 三次均通过。
- 无 `case_id`、完整题面或答案模板硬编码。

## 10. 子 Agent 开发与审查

实施采用串行的 Subagent-Driven Development：

1. 每个任务由新的实现子 Agent 按 TDD 完成。
2. 每个任务完成后由独立审查子 Agent 检查规格符合性和代码质量。
3. Critical/Important 问题修复后必须重新审查。
4. 全部任务完成后，由未参与实现的高能力子 Agent 审查完整 diff、测试证据和评测产物。

“达到秋招强竞争力”必须同时满足：

- 代码审查通过；
- 指标在三次真实运行中稳定；
- 没有明显评测题硬编码；
- 失败与能力边界表述诚实；
- Redis/MySQL Golden Chain 能在面试演示中解释证据、来源职责和安全边界。

## 11. 风险与控制

- **Judge 波动**：固定温度和资产，重复三次，保留案例级结果并进行独立复核。
- **题库过拟合**：增加同义改写、负例和来源缺失案例，禁止 `case_id` 分支。
- **上下文不足**：按子目标公平分配预算，必要来源不足时失败关闭。
- **回归风险**：保留兼容接口，先定向测试再全量测试。
- **现有脏工作区**：用户已明确批准在 `main` 直接改进；只提交本轮明确新增或修改的文件，
  不覆盖、不暂存、不回退其他现有修改。
