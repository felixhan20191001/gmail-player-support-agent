from pathlib import Path

from player_support_agent.run_result_summary import build_run_result_summary
from player_support_agent.tools.config import NotifyConfig, SupportAgentConfig


def test_build_run_result_summary_includes_email_and_handoff_details(tmp_path: Path):
    handoff_dir = tmp_path / "handoffs"
    handoff_dir.mkdir()
    (handoff_dir / "m1.txt").write_text("handoff", encoding="utf-8")

    config = SupportAgentConfig(
        notify=NotifyConfig(mode="file", output_dir=str(handoff_dir)),
    )
    result = {
        "status": "human_review",
        "candidate_count": 4,
        "selected_count": 1,
        "outcomes": [
            {
                "message_id": "m1",
                "thread_id": "m1",
                "project_label": "BlackHole",
                "matched_labels": ["BlackHole"],
                "status": "human_review",
                "labels_applied": ["BlackHole", "BlackHole/广告问题"],
                "human_review_reason": "需要人工复核",
            }
        ],
        "answer": "自动处理已保存 1 个 case 状态。",
    }
    summary = build_run_result_summary(
        result,
        support_config=config,
        email_metadata={
            "m1": {
                "subject": "Ad freezing issue",
                "from": "Kim <kim@example.com>",
                "snippet": "The adds keep freezing",
            }
        },
        case_data_by_id={
            "m1": {
                "issue_type": "ad_issue",
                "language_source_text": "The adds keep freezing",
            }
        },
    )

    text = summary["text"]
    assert "已转人工处理" in text
    assert "BlackHole" in text
    assert "Ad freezing issue" in text
    assert "Kim <kim@example.com>" in text
    assert "广告问题" in text
    assert "需要人工复核" in text
    assert str(handoff_dir / "m1.txt") in text


def test_build_run_result_summary_uses_case_data_project_when_outcome_missing(tmp_path: Path):
    config = SupportAgentConfig(
        notify=NotifyConfig(mode="file", output_dir=str(tmp_path / "handoffs")),
    )
    result = {
        "status": "human_review",
        "candidate_count": 1,
        "selected_count": 1,
        "outcomes": [
            {
                "message_id": "m1",
                "thread_id": "m1",
                "project_label": None,
                "status": "human_review",
            }
        ],
    }
    summary = build_run_result_summary(
        result,
        support_config=config,
        case_data_by_id={"m1": {"project_label": "Grill Master", "issue_type": "ad_issue"}},
    )

    assert "Grill Master" in summary["text"]
    assert "未知项目" not in summary["text"]


def test_build_run_result_summary_lists_already_processed_candidates(tmp_path: Path):
    config = SupportAgentConfig(
        notify=NotifyConfig(mode="file", output_dir=str(tmp_path / "handoffs")),
    )
    result = {
        "status": "already_processed",
        "candidate_count": 2,
        "selected_count": 0,
        "candidates": [
            {"message_id": "m1", "thread_id": "m1", "project_label": "BlackHole"},
            {"message_id": "m2", "thread_id": "m2", "project_label": "BlackHole"},
        ],
        "skipped_details": [
            {
                "message_id": "m1",
                "store_status": "draft_created",
                "gmail_unread": True,
                "reason": "terminal_already_processed",
            },
            {
                "message_id": "m2",
                "store_status": "human_review",
                "gmail_unread": False,
                "reason": "terminal_already_processed",
            },
        ],
    }
    summary = build_run_result_summary(
        result,
        support_config=config,
        email_metadata={
            "m1": {"subject": "Payment issue", "from": "Kim <kim@example.com>"},
            "m2": {"subject": "Ad freeze", "from": "Mercedes <m@example.com>"},
        },
    )

    text = summary["text"]
    assert summary["headline"] == "候选均已处理"
    assert "候选 2 封，实际处理 0 封" in text
    assert "Payment issue" in text
    assert "Ad freeze" in text
    assert "本地已有终态记录" in text
    assert "没有新的待处理邮件" not in text
    assert "Gmail 仍为未读的邮件会进入处理" in text