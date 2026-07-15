# Resume Metric Rules

Only use a metric when its scorecard module records the run ID, evidence level, sample count,
raw artifact path, and failed-case detail.

Recommended wording:

> Built a provenance-rich RAG and AIOps evaluation system over multi-format knowledge assets,
> reporting retrieval, answer-quality, RCA, safety, and latency metrics from reproducible
> benchmark runs, with controlled local fault experiments and explicit production boundaries.

Current RAG-focused wording:

> Built a provenance-bearing OnCall RAG pipeline over 20 multi-format knowledge assets,
> separating deterministic retrieval, fixed-context generation, and runtime end-to-end
> evaluation. The frozen runtime retrieval set passed 20/20 with ID context recall 1.0000
> and OOD refusal 100%; Redis/MySQL/OOD stream and non-stream demo paths passed 3/3, while
> failed actionability and context-completeness cases remained visible as candidate evidence.

Short Chinese version:

> 围绕 20 份多格式运维知识资产构建可追溯 RAG 工程，将确定性检索、固定上下文生成和
> runtime 端到端评测分层；冻结检索集 `20/20`、ID recall `1.0000`、OOD 拒答 `100%`，
> Redis/MySQL/OOD 流式与非流式演示 `3/3`，并保留 actionability 与上下文缺口失败案例。

Do not claim production accuracy, production MTTD/MTTR improvement, or stable concurrency
capacity unless the scorecard contains sufficient `production` or formal load-test samples.

Do not call the current RAG Scorecard official. It is generated from a dirty worktree and
contains a failed fixed-context/full-runtime quality contract.
