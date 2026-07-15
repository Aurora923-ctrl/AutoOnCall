# AutoOnCall 五分钟 RAG 演示

演示前运行：

```powershell
make up
make upload
.venv\Scripts\python.exe scripts\eval\verify_rag_demo_cases.py
.venv\Scripts\python.exe scripts\eval\build_rag_scorecard.py
```

当前 Scorecard 是 dirty-worktree `candidate`，不能说成 official。

默认 AIOps 主线先运行：

```powershell
make interview-up
make sandbox-verify
.venv\Scripts\python.exe scripts\eval\eval_cases.py --cases eval\cases.yaml --env-file deploy\sandbox.env --skip-rag --live-golden
```

`--skip-rag` 只表示 live AIOps run 不重复计算 RAG 指标；RAG 结果来自自己的 retrieval/RAGAS Artifact。兼容面试汇总路径为 `logs/interview_eval_summary.md`。

| 时间 | 展示内容 | 讲解重点 |
| --- | --- | --- |
| `0:00-0:30` | `logs/rag_scorecard_candidate.md` | 三层评测：deterministic retrieval、fixed-context generation、runtime end-to-end |
| `0:30-1:30` | Redis 冻结案例 | 事故复盘与 Redis 官方限制同时引用；历史 incident-window 不等于当前实时事实 |
| `1:30-2:20` | MySQL 冻结案例 | slow-query digest、只读 EXPLAIN、变更审批和窗口 |
| `2:20-3:00` | OOD 拒答 | 流式/非流式均为空 citations，policy 为 `refuse_without_trusted_source` |
| `3:00-3:50` | CPU 失败案例 | 当前 chunk 只支持取证，不支持完整处置边界；不让模型补造审批和回滚条件 |
| `3:50-4:30` | 延迟与 Token | `qwen-max`、P50/P95、provider Token；金额因无价格快照保持 `not_observed` |
| `4:30-5:00` | 边界总结 | runtime id-smoke 是 `9/12`，冻结演示 `3/3`；平均分不能掩盖单 case failure |

必须主动说明：

- 在默认五分钟 AIOps 主线中，Milvus/RAG 是加分项；本页是需要展开知识链路时使用的专项演示，不替代 Redis/MySQL live adapter 主线。
- K8s CrashLoop/OOMKilled 当前是离线黄金回归用例，不宣称已经完成 live K8s 故障注入。
- Runtime frozen retrieval 是 `20/20`，ID recall 是 `1.0000`，OOD 是 `100%`。
- Fixed-context generation 虽有 Faithfulness `0.9166`、Relevancy `0.8408`，完整 contract
  仍只有 `2/12`。
- Runtime id-smoke 为 `9/12`，Disk、MySQL、Kubernetes actionability rubric 未通过。
- 冻结三案例流式/非流式为 `3/3`，双路径真实计时 `35.86s`，不是生产容量结论。
- AIOps live adapter scorecard 与本 RAG Scorecard 分开，不跨 commit、证据层级或 run 混用数字。
