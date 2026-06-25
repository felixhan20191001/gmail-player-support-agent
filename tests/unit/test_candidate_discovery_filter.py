import pytest

from player_support_agent.candidate_discovery_filter import (
    SKIP_CATEGORY_NON_PROJECT,
    candidate_has_project_association,
    partition_player_feedback_candidates,
    project_name_in_subject,
    sender_matches_non_project_patterns,
    should_skip_as_non_project,
)
from player_support_agent.processed_message_store import ProcessedMessageStore
from player_support_agent.tools.config import GmailConfig


def test_sender_matches_non_project_patterns_for_email_and_domain():
    patterns = ["email.anthropic.com", "notice@email.anthropic.com"]

    assert sender_matches_non_project_patterns(
        "Anthropic <notice@email.anthropic.com>",
        patterns,
    )
    assert sender_matches_non_project_patterns(
        "Other <ops@email.anthropic.com>",
        patterns,
    )
    assert not sender_matches_non_project_patterns(
        "Player <player@example.com>",
        patterns,
    )


def test_project_name_in_subject_detects_configured_project():
    assert project_name_in_subject(
        "BlackHole payment issue",
        ["BlackHole", "Number Sum"],
    )
    assert not project_name_in_subject("Eat Everything", ["BlackHole"])


def test_should_skip_as_non_project_without_label_or_subject_match():
    gmail_config = GmailConfig(
        project_label_names=["BlackHole"],
        skip_non_project_candidates=True,
        non_project_sender_patterns=["email.anthropic.com"],
    )
    candidate = {"message_id": "m1", "thread_id": "m1"}
    metadata = {
        "subject": "Eat Everything",
        "from": "Anthropic <notice@email.anthropic.com>",
        "project_labels": [],
        "label_names": ["INBOX", "UNREAD"],
    }

    assert should_skip_as_non_project(candidate, metadata, gmail_config) is True


def test_should_keep_candidate_with_project_label():
    gmail_config = GmailConfig(
        project_label_names=["BlackHole"],
        skip_non_project_candidates=True,
    )
    candidate = {
        "message_id": "m1",
        "thread_id": "m1",
        "project_label": "BlackHole",
    }

    assert should_skip_as_non_project(candidate, {}, gmail_config) is False


def test_candidate_has_project_association_from_matched_labels():
    assert candidate_has_project_association(
        {
            "message_id": "m1",
            "matched_labels": ["BlackHole/广告问题"],
        },
        ["BlackHole"],
    )


@pytest.mark.asyncio
async def test_partition_player_feedback_candidates_filters_non_project_mail():
    class FakeGmail:
        async def get_message_discovery_metadata(self, message_ids):
            return {
                "m1": {
                    "subject": "Eat Everything",
                    "from": "Anthropic <notice@email.anthropic.com>",
                    "project_labels": [],
                    "label_names": ["INBOX", "UNREAD"],
                },
                "m2": {
                    "subject": "BlackHole ad issue",
                    "from": "Player <player@example.com>",
                    "project_labels": ["BlackHole"],
                    "label_names": ["BlackHole", "INBOX", "UNREAD"],
                },
            }

        async def aclose(self):
            return None

    gmail_config = GmailConfig(
        project_label_names=["BlackHole"],
        skip_non_project_candidates=True,
        non_project_sender_patterns=["email.anthropic.com"],
    )
    candidates = [
        {"message_id": "m1", "thread_id": "m1"},
        {
            "message_id": "m2",
            "thread_id": "m2",
            "project_label": "BlackHole",
        },
    ]

    kept, skipped = await partition_player_feedback_candidates(
        gmail_config,
        candidates,
        gmail=FakeGmail(),
    )

    assert [item["message_id"] for item in kept] == ["m2"]
    assert skipped[0]["message_id"] == "m1"


def test_select_candidates_skips_non_project_even_when_gmail_unread(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [{"message_id": "m1", "thread_id": "m1"}]
    store.record_seen(candidates)
    store.mark_non_project_ignored(
        [
            {
                "message_id": "m1",
                "thread_id": "m1",
                "discovery_metadata": {
                    "subject": "Eat Everything",
                    "from": "notice@email.anthropic.com",
                },
            }
        ]
    )

    selected, skipped = store.select_candidates_for_run(
        candidates,
        limit=1,
        retry_failed=False,
        max_retries=3,
        ignore_store=False,
        unread_message_ids={"m1"},
    )

    assert selected == []
    assert skipped[0]["reason"] == "non_project_mail"
    assert store._read()["messages"]["m1"]["skip_category"] == SKIP_CATEGORY_NON_PROJECT