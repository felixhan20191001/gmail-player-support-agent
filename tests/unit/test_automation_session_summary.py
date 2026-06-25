from player_support_agent.automation_session_summary import (
    CycleSnapshot,
    aggregate_session,
    build_automation_feed,
    build_session_started_message,
)


def test_aggregate_session_counts_processed_and_outcomes():
    cycles = [
        CycleSnapshot(
            run_id="r1",
            created_at="2026-06-15T10:00:00+00:00",
            status="draft_created",
            candidate_count=3,
            selected_count=1,
            outcome_count_by_status={"draft_created": 1},
        ),
        CycleSnapshot(
            run_id="r2",
            created_at="2026-06-15T10:05:00+00:00",
            status="skipped",
            candidate_count=0,
            selected_count=0,
        ),
        CycleSnapshot(
            run_id="r3",
            created_at="2026-06-15T10:10:00+00:00",
            status="human_review",
            candidate_count=2,
            selected_count=1,
            outcome_count_by_status={"human_review": 1},
        ),
    ]

    summary = aggregate_session(cycles)

    assert summary["cycle_count"] == 3
    assert summary["processed_count"] == 2
    assert summary["outcome_by_status"] == {
        "draft_created": 1,
        "human_review": 1,
    }
    assert summary["empty_rounds"] == 1
    assert summary["last_cycle_at"] == "2026-06-15T10:10:00+00:00"


def test_build_automation_feed_returns_history_without_after_cursor():
    cycles = [
        CycleSnapshot(
            run_id="r1",
            created_at="2026-06-15T10:00:00+00:00",
            status="draft_created",
            selected_count=1,
            headline="已创建草稿",
            text="本轮结果：已创建草稿",
        ),
        CycleSnapshot(
            run_id="r2",
            created_at="2026-06-15T10:05:00+00:00",
            status="skipped",
            headline="无新邮件",
            text="本轮结果：无新邮件",
        ),
    ]
    feed = build_automation_feed(
        running=True,
        session_id="auto-session-1",
        started_at="2026-06-15T09:59:00+00:00",
        live_run=False,
        interval_seconds=300,
        cycles=cycles,
        after_run_id=None,
        include_history=True,
    )

    assert feed["summary"]["processed_count"] == 1
    assert feed["events"][0]["type"] == "session_started"
    assert [event["run_id"] for event in feed["events"] if event["type"] == "cycle_done"] == [
        "r1",
        "r2",
    ]


def test_build_automation_feed_filters_events_after_run_id():
    cycles = [
        CycleSnapshot(
            run_id="r1",
            created_at="2026-06-15T10:00:00+00:00",
            status="draft_created",
            text="first",
        ),
        CycleSnapshot(
            run_id="r2",
            created_at="2026-06-15T10:05:00+00:00",
            status="skipped",
            text="second",
        ),
    ]
    feed = build_automation_feed(
        running=True,
        session_id="auto-session-1",
        started_at="2026-06-15T09:59:00+00:00",
        live_run=True,
        interval_seconds=300,
        cycles=cycles,
        after_run_id="r1",
        include_history=False,
    )

    assert [event.get("run_id") for event in feed["events"]] == ["r2"]


def test_build_session_started_message_mentions_mode_and_interval():
    message = build_session_started_message(
        session_id="auto-session-abc",
        started_at="2026-06-15T10:00:00+00:00",
        live_run=True,
        interval_seconds=120,
    )

    assert "Live 草稿" in message
    assert "120s" in message
    assert "auto-session-abc" in message