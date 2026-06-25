import pytest

from player_support_agent.dry_run import SIDE_EFFECT_TOOLS, apply_dry_run
from player_support_agent.tools.config import SupportAgentConfig
from player_support_agent.tools.forge_tools import build_tool_defs


@pytest.mark.asyncio
async def test_apply_dry_run_blocks_gmail_write_tools():
    tools = apply_dry_run(build_tool_defs(SupportAgentConfig()), allow_db=False)

    labels = await tools["apply_existing_gmail_labels"].callable(
        message_ids=["m1"],
        label_names=["NumberCrush/bug反馈"],
    )
    draft = await tools["create_gmail_draft"].callable(
        to="player@example.com",
        subject="Re: test",
        body="Thanks for reaching out.",
        thread_id="t1",
    )

    assert labels["dry_run"] is True
    assert labels["tool"] == "apply_existing_gmail_labels"
    assert draft["dry_run"] is True
    assert draft["tool"] == "create_gmail_draft"


def test_apply_dry_run_keeps_read_email_thread_live():
    tools = apply_dry_run(build_tool_defs(SupportAgentConfig()), allow_db=False)

    assert "read_email_thread" not in SIDE_EFFECT_TOOLS
    assert tools["read_email_thread"].callable.__name__ == "read_email_thread"
    assert tools["apply_existing_gmail_labels"].callable.__name__ == "async_wrapper"


def test_auto_workflow_applies_dry_run_wrappers():
    from player_support_agent.workflows import build_multi_project_workflow

    workflow = build_multi_project_workflow(SupportAgentConfig(), dry_run=True)

    assert workflow.name == "multi_project_support"
    assert workflow.terminal_tool == "save_case_state"
    assert "respond" not in workflow.tools


@pytest.mark.asyncio
async def test_auto_worker_sorts_candidates_newest_first(monkeypatch):
    from player_support_agent import auto_worker

    class FakeGmailTools:
        def __init__(self, config):
            pass

        async def list_unread_project_emails(self, max_results_per_label=10):
            return {
                "messages": [
                    {"message_id": "older", "thread_id": "t-old"},
                    {"message_id": "newer", "thread_id": "t-new"},
                ]
            }

        async def get_message_internal_dates(self, message_ids):
            return {
                "older": "1000",
                "newer": "2000",
            }

    monkeypatch.setattr(auto_worker, "GmailTools", FakeGmailTools)
    candidates = await auto_worker.fetch_new_message_ids(
        SupportAgentConfig(),
        max_results=10,
    )

    assert [item["message_id"] for item in candidates] == ["newer", "older"]