from player_support_agent.processed_message_store import ProcessedMessageStore


def _candidate(message_id: str) -> dict:
    return {
        "message_id": message_id,
        "thread_id": message_id,
        "project_label": "BlackHole",
    }


def test_select_candidates_reprocesses_unread_terminal(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [_candidate("m1"), _candidate("m2")]
    store.record_seen(candidates)
    data = store._read()
    data["messages"]["m1"]["status"] = "draft_created"
    data["messages"]["m2"]["status"] = "human_review"
    store._write(data)

    selected, skipped = store.select_candidates_for_run(
        candidates,
        limit=1,
        retry_failed=False,
        max_retries=3,
        ignore_store=False,
        unread_message_ids={"m1", "m2"},
    )

    assert [item["message_id"] for item in selected] == ["m1"]
    assert selected[0]["reprocess_gmail_unread"] is True
    assert skipped == []


def test_select_candidates_skips_terminal_when_gmail_read(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [_candidate("m1"), _candidate("m2")]
    store.record_seen(candidates)
    data = store._read()
    data["messages"]["m1"]["status"] = "draft_created"
    data["messages"]["m2"]["status"] = "skipped"
    store._write(data)

    selected, skipped = store.select_candidates_for_run(
        candidates,
        limit=2,
        retry_failed=False,
        max_retries=3,
        ignore_store=False,
        unread_message_ids=set(),
    )

    assert selected == []
    assert len(skipped) == 2
    assert {item["message_id"] for item in skipped} == {"m1", "m2"}
    assert all(item["reason"] == "terminal_already_processed" for item in skipped)
    assert all(item["gmail_unread"] is False for item in skipped)


def test_select_candidates_skips_processing_status(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [_candidate("m1")]
    store.record_seen(candidates)
    data = store._read()
    data["messages"]["m1"]["status"] = "processing"
    store._write(data)

    selected, skipped = store.select_candidates_for_run(
        candidates,
        limit=1,
        retry_failed=False,
        max_retries=3,
        ignore_store=False,
        unread_message_ids={"m1"},
    )

    assert selected == []
    assert skipped == [
        {
            "message_id": "m1",
            "store_status": "processing",
            "gmail_unread": True,
            "reason": "processing_in_progress",
        }
    ]


def test_select_candidates_reprocesses_unread_failed_without_retry_failed(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [_candidate("m1")]
    store.record_seen(candidates)
    data = store._read()
    data["messages"]["m1"]["status"] = "failed"
    data["messages"]["m1"]["retry_count"] = 3
    store._write(data)

    selected, skipped = store.select_candidates_for_run(
        candidates,
        limit=1,
        retry_failed=False,
        max_retries=3,
        ignore_store=False,
        unread_message_ids={"m1"},
    )

    assert [item["message_id"] for item in selected] == ["m1"]
    assert selected[0]["reprocess_gmail_unread"] is True
    assert skipped == []


def test_select_candidates_skips_failed_when_gmail_read_and_retry_disabled(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [_candidate("m1")]
    store.record_seen(candidates)
    data = store._read()
    data["messages"]["m1"]["status"] = "failed"
    store._write(data)

    selected, skipped = store.select_candidates_for_run(
        candidates,
        limit=1,
        retry_failed=False,
        max_retries=3,
        ignore_store=False,
        unread_message_ids=set(),
    )

    assert selected == []
    assert skipped[0]["reason"] == "failed_retry_disabled"


def test_select_candidates_ignore_store_unchanged(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [_candidate("m1"), _candidate("m2")]
    store.record_seen(candidates)
    data = store._read()
    data["messages"]["m1"]["status"] = "draft_created"
    store._write(data)

    selected, skipped = store.select_candidates_for_run(
        candidates,
        limit=1,
        retry_failed=False,
        max_retries=3,
        ignore_store=True,
        unread_message_ids=set(),
    )

    assert [item["message_id"] for item in selected] == ["m1"]
    assert skipped == []