"""Tests for safe logging helpers."""

from app.utils.log_safety import summarize_text_for_log


def test_summarize_text_for_log_omits_raw_content() -> None:
    summary = summarize_text_for_log("password=secret Redis timeout", label="question")

    assert "question_len=" in summary
    assert "question_sha256=" in summary
    assert "password" not in summary
    assert "secret" not in summary
    assert "Redis timeout" not in summary
