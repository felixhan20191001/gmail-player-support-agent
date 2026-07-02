from datetime import datetime, timedelta, timezone

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


def test_select_candidates_includes_existing_draft_for_unread_reprocess(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [_candidate("m1")]
    store.record_seen(candidates)
    data = store._read()
    data["messages"]["m1"].update(
        {
            "status": "draft_created",
            "draft_id": "draft-1",
            "labels_applied": [],
            "data": {
                "draft_id": "draft-1",
                "case_type": "crash_or_freeze",
                "recommended_labels": ["BlackHole", "BlackHole/崩溃卡死"],
                "labels_applied": ["BlackHole", "BlackHole/崩溃卡死"],
                "language_source_text": "private player text must stay out",
            },
        }
    )
    store._write(data)

    selected, skipped = store.select_candidates_for_run(
        candidates,
        limit=1,
        retry_failed=False,
        max_retries=3,
        ignore_store=False,
        unread_message_ids={"m1"},
    )

    assert skipped == []
    assert selected == [
        {
            "message_id": "m1",
            "thread_id": "m1",
            "project_label": "BlackHole",
            "reprocess_gmail_unread": True,
            "existing_status": "draft_created",
            "existing_draft_id": "draft-1",
            "existing_issue_type": "crash_or_freeze",
            "existing_recommended_labels": ["BlackHole", "BlackHole/崩溃卡死"],
            "existing_labels_applied": [],
        }
    ]
    assert "language_source_text" not in selected[0]


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


def test_select_candidates_skips_unread_failed_without_retry_failed(tmp_path):
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

    assert selected == []
    assert skipped == [
        {
            "message_id": "m1",
            "store_status": "failed",
            "gmail_unread": True,
            "reason": "failed_retry_disabled",
        }
    ]


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


def test_select_candidates_only_retries_unread_failed_when_requested(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [_candidate("m1")]
    store.record_seen(candidates)
    data = store._read()
    data["messages"]["m1"]["status"] = "failed"
    data["messages"]["m1"]["retry_count"] = 0
    store._write(data)

    selected, skipped = store.select_candidates_for_run(
        candidates,
        limit=1,
        retry_failed=False,
        max_retries=3,
        ignore_store=False,
        unread_message_ids={"m1"},
        reprocess_failed_unread=False,
    )

    assert selected == []
    assert skipped[0]["reason"] == "failed_retry_disabled"
    assert skipped[0]["gmail_unread"] is True

    selected2, skipped2 = store.select_candidates_for_run(
        candidates,
        limit=1,
        retry_failed=True,
        max_retries=3,
        ignore_store=False,
        unread_message_ids={"m1"},
        reprocess_failed_unread=False,
    )
    assert [item["message_id"] for item in selected2] == ["m1"]
    assert skipped2 == []

    selected3, skipped3 = store.select_candidates_for_run(
        candidates,
        limit=1,
        retry_failed=False,
        max_retries=3,
        ignore_store=False,
        unread_message_ids={"m1"},
        # reprocess_failed_unread defaults to False; failed mail needs explicit retry.
    )
    assert selected3 == []
    assert skipped3[0]["reason"] == "failed_retry_disabled"


def test_recover_stale_processing_marks_only_expired_records_failed(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [_candidate("old"), _candidate("fresh"), _candidate("done")]
    store.record_seen(candidates)
    data = store._read()
    now = datetime.now(timezone.utc)
    data["messages"]["old"].update(
        {
            "status": "processing",
            "agent_run_id": "run-old",
            "last_processed_at": (now - timedelta(minutes=180)).isoformat(),
        }
    )
    data["messages"]["fresh"].update(
        {
            "status": "processing",
            "agent_run_id": "run-fresh",
            "last_processed_at": (now - timedelta(minutes=30)).isoformat(),
        }
    )
    data["messages"]["done"].update(
        {
            "status": "draft_created",
            "agent_run_id": "run-done",
            "last_processed_at": (now - timedelta(minutes=180)).isoformat(),
        }
    )
    store._write(data)

    recovered = store.recover_stale_processing(
        stale_after_minutes=120,
        target_status="failed",
    )

    assert recovered == [
        {
            "message_id": "old",
            "thread_id": "old",
            "previous_run_id": "run-old",
            "previous_status": "processing",
            "new_status": "failed",
        }
    ]
    data = store._read()
    assert data["messages"]["old"]["status"] == "failed"
    assert "Stale processing recovered" in data["messages"]["old"]["error_message"]
    assert data["messages"]["fresh"]["status"] == "processing"
    assert data["messages"]["done"]["status"] == "draft_created"


def test_recover_stale_processing_can_restore_pending_without_error(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    store.record_seen([_candidate("old")])
    data = store._read()
    data["messages"]["old"].update(
        {
            "status": "processing",
            "agent_run_id": "run-old",
            "last_processed_at": "2026-07-01T00:00:00+00:00",
            "error_message": None,
        }
    )
    store._write(data)

    recovered = store.recover_stale_processing(
        stale_after_minutes=1,
        target_status="pending",
        now=datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc),
    )

    assert recovered[0]["new_status"] == "pending"
    data = store._read()
    assert data["messages"]["old"]["status"] == "pending"
    assert data["messages"]["old"]["error_message"] is None
