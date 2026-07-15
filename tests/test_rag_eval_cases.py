"""Tests for offline RAG retrieval evaluation cases."""

from pathlib import Path

from scripts.eval.eval_rag_cases import (
    _strategy_case_payload,
    build_offline_index,
    evaluate_case,
    evaluate_cases,
    load_cases,
    render_summary,
    search_offline,
)


def test_rag_cases_cover_core_runbook_types_and_rejection() -> None:
    cases = load_cases("eval/rag_cases.yaml")
    case_ids = {case["id"] for case in cases}

    assert "cpu_high_usage_alert" in case_ids
    assert "memory_oom" in case_ids
    assert "disk_no_space" in case_ids
    assert "service_503_unavailable" in case_ids
    assert "slow_response_sql" in case_ids
    assert "cpu_high_but_root_cause_slow_query" in case_ids
    assert "service_503_but_dependency_timeout" in case_ids
    assert "reject_stock_investment" in case_ids
    assert "reject_resume_question" in case_ids
    assert "pdf_postmortem_loader_metadata" in case_ids
    assert "html_wiki_loader_heading" in case_ids
    assert "table_ticket_loader_row_citation" in case_ids
    assert "xlsx_deploy_history_row_citation" in case_ids
    assert len(cases) >= 20

    reject_cases = [case for case in cases if case.get("should_reject")]
    assert len(reject_cases) >= 5


def test_stage_two_dataset_does_not_mislabel_tuned_cases_as_holdout() -> None:
    cases = load_cases("eval/rag_relevance_cases.yaml")

    assert len(cases) == 80
    assert {case["split"] for case in cases} == {"dev", "regression"}
    assert not [case for case in cases if case["split"] == "holdout"]
    for case in cases:
        if case.get("should_reject"):
            continue
        assert case["required_sources"]
        assert case["relevant_chunks"]


def test_stage_two_frozen_holdout_has_required_shape() -> None:
    cases = load_cases("eval/rag_holdout_cases.yaml")

    assert len(cases) == 30
    assert {case["split"] for case in cases} == {"holdout"}
    assert len([case for case in cases if not case.get("should_reject")]) == 24
    assert len([case for case in cases if case.get("should_reject")]) == 6


def test_rag_eval_cases_report_current_untuned_baseline() -> None:
    payload = evaluate_cases("eval/rag_cases.yaml", docs_dir="docs/knowledge-base")

    assert payload["summary"]["case_count"] == 30
    assert payload["summary"]["passed_count"] == 29
    assert payload["summary"]["pass_rate"] == 0.9667
    assert payload["summary"]["recall_at_k"] == 0.96
    assert payload["summary"]["citation_coverage_rate"] == 0.96
    assert payload["summary"]["no_answer_rejection_rate"] == 1.0
    assert payload["summary"]["confusion_case_pass_rate"] == 0.8
    assert payload["summary"]["reject_case_count"] >= 5
    assert payload["summary"]["confusion_case_count"] >= 4
    assert payload["summary"]["mrr"] >= 0.9
    assert payload["summary"]["strategy_metrics"]["weighted"]["recall_at_k"] == 0.96
    assert "rrf" in payload["summary"]["strategy_metrics"]
    assert payload["run"]["fusion_strategies"] == [
        "weighted",
        "rrf",
        "lexical-only",
        "vector-only",
    ]
    assert payload["summary"]["recall_at_5"] == 1.0
    assert 0.0 <= payload["summary"]["precision_at_3"] <= 1.0
    assert 0.0 <= payload["summary"]["map_at_3"] <= 1.0
    assert 0.0 <= payload["summary"]["ndcg_at_3"] <= 1.0
    assert payload["summary"]["no_answer_rejection_precision"] == 1.0
    assert payload["summary"]["no_answer_rejection_recall"] == 1.0
    assert payload["summary"]["no_answer_rejection_f1"] == 1.0
    assert payload["summary"]["latency_ms"]["p50"] >= 0.0
    assert payload["summary"]["latency_ms"]["p95"] >= payload["summary"]["latency_ms"]["p50"]
    assert payload["summary"]["latency_ms"]["p99"] >= payload["summary"]["latency_ms"]["p95"]
    assert "dev" in payload["summary"]["slices"]["split"]
    assert payload["summary"]["metrics"]["recall_at_3"]["sample_count"] == 25
    assert "confidence_interval" in payload["summary"]["metrics"]["recall_at_3"]

    summary_text = render_summary(payload)
    assert "RAG eval: 29/30 cases passed" in summary_text
    assert "Strategy comparison:" in summary_text
    assert "recall@3=96%" in summary_text
    assert "retrieval_citation_metadata=96%" in summary_text
    assert "confusion=80%" in summary_text
    assert "reject=100%" in summary_text

    for result in payload["cases"]:
        if result["id"] == "cpu_slow_sql_relation":
            assert result["passed"] is False
            assert result["failed_metrics"] == [
                "recall_at_k",
                "keyword_hit",
                "citation_coverage",
            ]
            continue
        assert result["failed_metrics"] == []
        assert result["failure_reasons"] == {}
        if not result["should_reject"]:
            assert result["citation_hit"] is True
            assert "weighted" in result["strategy_results"]
            assert "rrf" in result["strategy_results"]
            assert "lexical-only" in result["strategy_results"]
            assert "vector-only" in result["strategy_results"]


def test_rag_eval_offline_strategy_comparison_can_rank_by_rrf() -> None:
    index = [
        {
            "source_file": "weaker.md",
            "chunk_id": "weaker.md#0001",
            "content": "Redis timeout",
            "heading_path": "",
            "offline_terms": {"redis", "timeout"},
        },
        {
            "source_file": "stronger.md",
            "chunk_id": "stronger.md#0001",
            "content": "Redis timeout maxclients connection",
            "heading_path": "",
            "offline_terms": {"redis", "timeout", "maxclients", "connection"},
        },
    ]

    weighted = search_offline(
        index,
        "Redis timeout maxclients connection",
        top_k=1,
        min_score=0.1,
        fusion_strategy="weighted",
    )
    rrf = search_offline(
        index,
        "Redis timeout maxclients connection",
        top_k=1,
        min_score=0.1,
        fusion_strategy="rrf",
    )

    assert weighted[0]["source_file"] == "stronger.md"
    assert rrf[0]["source_file"] == "stronger.md"


def test_pure_strategy_scores_do_not_apply_intent_multiplier() -> None:
    index = [
        {
            "source_file": "cpu_high_usage.md",
            "chunk_id": "cpu#1",
            "content": "shared token",
            "heading_path": "",
            "offline_terms": {"shared", "token"},
        },
        {
            "source_file": "neutral.md",
            "chunk_id": "neutral#1",
            "content": "shared token",
            "heading_path": "",
            "offline_terms": {"shared", "token"},
        },
    ]

    for strategy in ("lexical-only", "vector-only"):
        results = search_offline(
            index,
            "CPU shared token",
            top_k=2,
            min_score=0.0,
            fusion_strategy=strategy,
        )
        scores = {item["source_file"]: item["offline_score"] for item in results}
        assert scores["cpu_high_usage.md"] == scores["neutral.md"]

    rrf = search_offline(
        index,
        "CPU shared token",
        top_k=2,
        min_score=0.0,
        fusion_strategy="rrf",
    )
    assert all(item["offline_score"] < 0.1 for item in rrf)
    assert {item["source_file"] for item in rrf} == {"cpu_high_usage.md", "neutral.md"}


def test_lexical_baseline_ignores_heading_intent_rules() -> None:
    index = [
        {
            "source_file": "intent.md",
            "chunk_id": "intent#1",
            "content": "shared evidence",
            "heading_path": "排查步骤",
            "offline_terms": {"shared", "evidence"},
        },
        {
            "source_file": "plain.md",
            "chunk_id": "plain#1",
            "content": "shared evidence",
            "heading_path": "Overview",
            "offline_terms": {"shared", "evidence"},
        },
    ]

    lexical = search_offline(
        index,
        "如何排查 shared evidence",
        top_k=2,
        min_score=0.0,
        fusion_strategy="lexical-only",
    )
    weighted = search_offline(
        index,
        "如何排查 shared evidence",
        top_k=2,
        min_score=0.0,
        fusion_strategy="weighted",
    )

    lexical_scores = {item["source_file"]: item["offline_score"] for item in lexical}
    assert lexical_scores["intent.md"] == lexical_scores["plain.md"]
    assert weighted[0]["source_file"] == "intent.md"


def test_pure_strategies_do_not_share_weighted_out_of_domain_rejection() -> None:
    index = [
        {
            "source_file": "general.md",
            "chunk_id": "general.md#1",
            "content": "旅行 行程",
            "heading_path": "",
            "offline_terms": {"旅行", "行程"},
        }
    ]

    weighted = search_offline(
        index,
        "旅行 行程",
        top_k=1,
        min_score=0.0,
        fusion_strategy="weighted",
    )
    lexical = search_offline(
        index,
        "旅行 行程",
        top_k=1,
        min_score=0.0,
        fusion_strategy="lexical-only",
    )
    vector = search_offline(
        index,
        "旅行 行程",
        top_k=1,
        min_score=0.0,
        fusion_strategy="vector-only",
    )
    rrf = search_offline(
        index,
        "旅行 行程",
        top_k=1,
        min_score=0.0,
        fusion_strategy="rrf",
    )

    assert weighted == []
    assert lexical[0]["source_file"] == "general.md"
    assert vector[0]["source_file"] == "general.md"
    assert rrf[0]["source_file"] == "general.md"


def test_search_offline_understands_alert_rule_and_symptom_paraphrases() -> None:
    index = build_offline_index("docs/knowledge-base")

    rule_results = search_offline(
        index,
        "PromQL 告警怎样设置持续时间、通知标签和说明字段？",
        top_k=3,
        min_score=0.5,
    )
    practice_results = search_offline(
        index,
        "为什么告警应优先覆盖用户可见影响，而不是枚举每个内部原因？",
        top_k=3,
        min_score=0.5,
    )

    assert "official_prometheus_alerting_rules.md#0002" in {
        item["chunk_id"] for item in rule_results
    }
    assert "official_prometheus_alerting_practices.md#0003" in {
        item["chunk_id"] for item in practice_results
    }


def test_search_offline_prefers_explicit_ticket_document_type() -> None:
    index = [
        {
            "source_file": "service_unavailable.md",
            "chunk_id": "generic#1",
            "heading_path": "Service unavailable",
            "content": "Redis retry loop maxclients INC-REDIS-009",
            "offline_terms": {"redis", "retry", "loop", "maxclients", "inc-redis-009"},
        },
        {
            "source_file": "tickets.csv",
            "chunk_id": "ticket#1",
            "heading_path": "",
            "content": "INC-REDIS-009 Redis retry loop maxclients resolution",
            "metadata": {"doc_type": "table"},
            "offline_terms": {"redis", "retry", "loop", "maxclients", "inc-redis-009"},
        },
    ]

    retrieved = search_offline(
        index,
        "INC-REDIS-009 Redis retry history maxclients",
        top_k=2,
        min_score=0.1,
    )

    assert retrieved[0]["source_file"] == "tickets.csv"


def test_search_offline_distinguishes_loki_query_from_ingestion_intent() -> None:
    index = build_offline_index("docs/knowledge-base")

    query_results = search_offline(
        index,
        "Loki 查询超时，应缩短时间范围还是扩大 ingester 配置？",
        top_k=3,
        min_score=0.5,
    )
    ingest_results = search_offline(
        index,
        "Loki 写入失败需要检查哪些 ingestion 指标？",
        top_k=3,
        min_score=0.5,
    )

    assert any(
        item["source_file"] == "official_loki_troubleshoot_query.md"
        for item in query_results
    )
    assert ingest_results[0]["source_file"] == "official_loki_troubleshoot_ingest.md"


def test_search_offline_prefers_runbook_steps_within_matching_source() -> None:
    index = build_offline_index("docs/knowledge-base")

    results = search_offline(
        index,
        "线上接口全部失败应参考哪份 Runbook？",
        top_k=3,
        min_score=0.5,
    )

    assert "service_unavailable.md#0003" in [item["chunk_id"] for item in results]


def test_search_offline_recalls_pod_and_service_debug_evidence() -> None:
    index = build_offline_index("docs/knowledge-base")

    results = search_offline(
        index,
        "Kubernetes 故障如何同时验证 Pod 与 Service 后端？",
        top_k=3,
        min_score=0.5,
    )

    sources = {item["source_file"] for item in results}
    assert "official_kubernetes_debug_pods.md" in sources
    assert "official_kubernetes_debug_services.md" in sources


def test_search_offline_combines_ingestion_metrics_and_alerting_principles() -> None:
    index = build_offline_index("docs/knowledge-base")

    results = search_offline(
        index,
        "可观测性写入故障如何结合指标与告警原则？",
        top_k=3,
        min_score=0.5,
    )

    sources = {item["source_file"] for item in results}
    assert "official_loki_troubleshoot_ingest.md" in sources
    assert "official_prometheus_alerting_practices.md" in sources


def test_search_offline_handles_enterprise_paraphrases() -> None:
    index = build_offline_index("docs/knowledge-base")

    memory = search_offline(
        index,
        "工作负载内存逼近限制时，首轮证据从哪里收集？",
        top_k=3,
        min_score=0.5,
    )
    redis = search_offline(
        index,
        "缓存节点客户端数接近限制，应查哪份官方说明？",
        top_k=3,
        min_score=0.5,
    )
    service = search_offline(
        index,
        "健康检查失败后应按哪些步骤开展诊断？",
        top_k=3,
        min_score=0.5,
    )
    mysql = search_offline(
        index,
        "支付服务数据库连接接近上限且查询变慢，应该查哪份知识页？",
        top_k=3,
        min_score=0.5,
    )

    assert "memory_high_usage.md#0003" in {item["chunk_id"] for item in memory}
    assert "official_redis_clients.md#0004" in {item["chunk_id"] for item in redis}
    assert "service_unavailable.md#0003" in {item["chunk_id"] for item in service}
    assert "payment_wiki.html#0001" in {item["chunk_id"] for item in mysql}


def test_rag_eval_case_failure_identifies_failed_metric() -> None:
    index = build_offline_index("docs/knowledge-base")
    result = evaluate_case(
        {
            "id": "bad_expected_source",
            "query": "billing-service CPU 使用率持续 95%",
            "expected_source": "missing_runbook.md",
            "expected_keywords": ["CPU"],
        },
        index,
        top_k=3,
        min_score=2.0,
    )

    assert result["passed"] is False
    assert result["failed_metrics"] == ["recall_at_k", "citation_coverage"]
    assert "Top-K 检索结果未命中" in result["failure_reasons"]["recall_at_k"]
    assert result["expected_sources"] == ["missing_runbook.md"]
    assert "cpu_high_usage.md" in result["retrieved_sources"]


def test_rag_eval_case_failure_identifies_missing_citation() -> None:
    result = evaluate_case(
        {
            "id": "missing_citation",
            "query": "Redis timeout",
            "expected_source": "redis.md",
            "expected_keywords": [],
        },
        [
            {
                "source_file": "",
                "chunk_id": "",
                "content": "Redis timeout runbook",
                "heading_path": "",
                "offline_terms": {"redis", "timeout"},
            }
        ],
        top_k=1,
        min_score=0.1,
    )

    assert result["passed"] is False
    assert "citation_coverage" in result["failed_metrics"]
    assert "缺少 source_file + chunk_id" in result["failure_reasons"]["citation_coverage"]


def test_load_cases_normalizes_stage_two_schema_and_legacy_fields(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(
        """
cases:
  - id: legacy
    query: Redis timeout
    expected_source: redis.md
  - id: graded
    query: MySQL pool waiting
    split: holdout
    category: mysql
    difficulty: hard
    required_sources: [mysql.md]
    acceptable_sources: [postmortem.pdf]
    forbidden_sources: [cpu.md]
    relevant_chunks:
      - chunk_id: mysql.md#2
        relevance: 3
      - id: postmortem.pdf#1
        grade: 1
""",
        encoding="utf-8",
    )

    legacy, graded = load_cases(path)

    assert legacy["split"] == "dev"
    assert legacy["required_sources"] == ["redis.md"]
    assert legacy["relevant_chunks"] == []
    assert graded["split"] == "holdout"
    assert graded["category"] == "mysql"
    assert graded["difficulty"] == "hard"
    assert graded["relevant_chunks"] == [
        {"chunk_id": "mysql.md#2", "relevance": 3},
        {"chunk_id": "postmortem.pdf#1", "relevance": 1},
    ]


def test_stage_two_case_metrics_use_chunk_grades_and_source_policies() -> None:
    index = [
        {
            "source_file": "mysql.md",
            "chunk_id": "mysql.md#1",
            "heading_path": "",
            "content": "MySQL pool waiting timeout primary",
            "offline_terms": {"mysql", "pool", "waiting", "timeout", "primary"},
        },
        {
            "source_file": "postmortem.pdf",
            "chunk_id": "postmortem.pdf#1",
            "heading_path": "",
            "content": "MySQL pool waiting timeout incident",
            "offline_terms": {"mysql", "pool", "waiting", "timeout", "incident"},
        },
        {
            "source_file": "cpu.md",
            "chunk_id": "cpu.md#1",
            "heading_path": "",
            "content": "MySQL pool waiting timeout CPU",
            "offline_terms": {"mysql", "pool", "waiting", "timeout", "cpu"},
        },
    ]
    result = evaluate_case(
        {
            "id": "graded",
            "query": "MySQL pool waiting timeout primary",
            "split": "holdout",
            "category": "mysql",
            "difficulty": "hard",
            "required_sources": ["mysql.md", "postmortem.pdf"],
            "acceptable_sources": [],
            "forbidden_sources": ["cpu.md"],
            "relevant_chunks": [
                {"chunk_id": "mysql.md#1", "relevance": 3},
                {"chunk_id": "postmortem.pdf#1", "relevance": 1},
            ],
            "expected_keywords": [],
        },
        index,
        top_k=3,
        min_score=0.1,
    )

    weighted = result["strategy_results"]["weighted"]
    assert result["split"] == "holdout"
    assert weighted["recall_at_k"]["1"] == 0.5
    assert weighted["recall_at_k"]["3"] == 1.0
    assert weighted["precision_at_k"]["3"] == 0.6667
    assert 0.0 <= weighted["map_at_k"]["3"] <= 1.0
    assert 0.0 <= weighted["ndcg_at_k"]["3"] <= 1.0
    assert weighted["strict_multisource_at_k"]["3"] is True
    assert weighted["forbidden_source_at_k"]["3"] is True
    assert result["passed"] is False
    assert result["failed_metrics"] == ["forbidden_source"]
    assert result["failure_ranking"][0]["relevance_grade"] == 3


def test_explicit_chunk_relevance_does_not_count_other_chunks_from_same_source() -> None:
    result = evaluate_case(
        {
            "id": "explicit_chunk_only",
            "query": "Redis maxclients",
            "required_sources": ["redis.md"],
            "relevant_chunks": [{"chunk_id": "redis.md#target", "relevance": 3}],
            "expected_keywords": [],
        },
        [
            {
                "source_file": "redis.md",
                "chunk_id": "redis.md#other",
                "content": "Redis maxclients overview",
                "heading_path": "",
                "offline_terms": {"redis", "maxclients", "overview"},
            },
            {
                "source_file": "redis.md",
                "chunk_id": "redis.md#target",
                "content": "Redis maxclients",
                "heading_path": "",
                "offline_terms": {"redis", "maxclients"},
            },
        ],
        top_k=1,
        min_score=0.1,
    )

    ranking = result["strategy_results"]["weighted"]["ranking"]
    assert any(
        item["chunk_id"] == "redis.md#other" and item["relevance_grade"] == 0 for item in ranking
    )
    assert result["strategy_results"]["weighted"]["recall_at_k"]["1"] <= 1.0
    assert result["strategy_results"]["weighted"]["recall_at_k"]["5"] <= 1.0


def test_duplicate_relevant_chunk_is_counted_once_for_all_ir_metrics() -> None:
    case = {
        "required_sources": ["redis.md"],
        "acceptable_sources": [],
        "forbidden_sources": [],
        "relevant_chunks": [{"chunk_id": "redis.md#target", "relevance": 3}],
        "expected_keywords": [],
    }
    duplicate = {
        "source_file": "redis.md",
        "chunk_id": "redis.md#target",
        "offline_score": 1.0,
    }

    payload = _strategy_case_payload([duplicate, duplicate, duplicate], case, latency_ms=1.0)

    assert payload["recall_at_k"]["3"] == 1.0
    assert payload["map_at_k"]["3"] == 1.0
    assert payload["ndcg_at_k"]["3"] == 1.0


def test_same_chunk_id_from_other_sources_is_not_counted_as_relevant() -> None:
    case = {
        "required_sources": ["redis.md"],
        "acceptable_sources": [],
        "forbidden_sources": [],
        "relevant_chunks": [{"chunk_id": "redis.md#target", "relevance": 3}],
        "expected_keywords": [],
    }
    retrieved = [
        {
            "source_file": source,
            "chunk_id": "redis.md#target",
            "offline_score": 1.0,
        }
        for source in ("redis.md", "other-a.md", "other-b.md")
    ]

    payload = _strategy_case_payload(retrieved, case, latency_ms=1.0)

    assert payload["recall_at_k"]["3"] == 1.0
    assert payload["map_at_k"]["3"] == 1.0
    assert payload["ndcg_at_k"]["3"] == 1.0


def test_strict_multisource_requires_relevant_chunk_from_each_source() -> None:
    case = {
        "required_sources": ["redis.md", "postmortem.pdf"],
        "acceptable_sources": [],
        "forbidden_sources": [],
        "relevant_chunks": [
            {"chunk_id": "redis.md#target", "relevance": 3},
            {"chunk_id": "postmortem.pdf#target", "relevance": 2},
        ],
        "expected_keywords": [],
    }
    retrieved = [
        {
            "source_file": "redis.md",
            "chunk_id": "redis.md#target",
            "offline_score": 2.0,
        },
        {
            "source_file": "postmortem.pdf",
            "chunk_id": "postmortem.pdf#wrong",
            "offline_score": 1.0,
        },
    ]

    payload = _strategy_case_payload(retrieved, case, latency_ms=1.0)

    assert payload["strict_multisource_at_k"]["3"] is False
