# AutoOnCall Five-Minute Demo

Milvus/RAG 是加分项；Redis/MySQL live adapter golden chain 是核心演示主线。

Before the demo, run `make interview-up` and `make sandbox-verify` to start and verify the
local adapter stack.
The live evaluator uses `--env-file deploy\sandbox.env` on Windows.

1. Run `make full-gate` before the interview, then open the existing workbench System view.
2. Start with the scorecard run ID, Git provenance, evidence levels, sample counts, failed cases, and raw artifact paths.
3. Show knowledge quality and Milvus consistency from the same benchmark run.
4. Show RAG retrieval metrics and one failed or degraded case instead of presenting only green averages.
5. Run the Redis or MySQL controlled-fault demo and follow Plan, parallel evidence, RCA, Trace, and report.
6. Show the approval, pre-check, dry-run, and rollback boundary for risky actions.
7. Show latency, token, cost, and capacity artifacts when those modules are available.
8. State explicitly that `production: not_enough_data`; controlled-fault MTTD/MTTR is not production MTTD/MTTR.

The scorecard source of truth is
`logs/benchmarks/<run_id>/interview_scorecard.json`. Every displayed number must trace back
to that run directory. `logs/interview_eval_summary.md` remains the legacy rollup entry for
the fixed demo package, but it must not override or mix the scorecard's benchmark run.

The default demo keeps K8s honest: K8s CrashLoop/OOMKilled 当前是离线黄金回归用例, not a
container-backed live fault in the core Redis/MySQL interview path. The live AIOps summary
may use `--skip-rag`; RAG and RAGAS remain separate quality artifacts.
