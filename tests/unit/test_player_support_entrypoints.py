import inspect
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from forge import Message, MessageMeta, MessageRole, MessageType
from forge.errors import ToolCallError

from player_support_agent import (
    agent_runner,
    auto_worker,
    chat_server,
    manual_trigger,
    prompts,
    terminal_chat,
    workflows,
)
from player_support_agent.agent_runner import (
    AgentRunConfig,
    AgentRunResult,
    CaseStatesComplete,
    ChatMemory,
    build_message_observer,
    build_user_message,
    extract_case_state_result,
    extract_draft_result,
    resolve_run_config,
    resolve_support_model_config,
    run_config_has_api_key,
)
from player_support_agent.auto_task_builder import build_auto_task
from player_support_agent.processed_message_store import (
    ProcessedMessageStore,
    format_auto_run_status_text,
    summarize_run_status,
)
from player_support_agent.tools.config import (
    ModelConfig,
    NotifyConfig,
    StateConfig,
    SupportAgentConfig,
)


def test_chat_server_does_not_route_by_keywords_or_call_gmail_stats():
    source = inspect.getsource(chat_server)

    assert "build_agent_command" not in source
    assert "gmail_stats" not in source
    assert "re.search" not in source
    assert "record_interactive_run" in source


def test_auto_workflow_requires_assess_before_decide():
    workflow = workflows.build_multi_project_workflow(SupportAgentConfig())

    assert "assess_claim_credibility" in workflow.required_steps
    assert workflow.required_steps.index("resolve_player_identity") < workflow.required_steps.index(
        "assess_claim_credibility"
    )
    assert workflow.required_steps.index("assess_claim_credibility") < workflow.required_steps.index(
        "decide_support_action"
    )


def test_auto_workflow_uses_smaller_tool_surface_than_chat():
    auto_workflow = workflows.build_multi_project_workflow(SupportAgentConfig())
    chat_workflow = workflows.build_multi_project_chat_workflow(SupportAgentConfig())

    auto_tools = set(auto_workflow.tools)
    chat_tools = set(chat_workflow.tools)

    assert auto_tools < chat_tools
    assert "respond" in chat_tools
    assert "respond" not in auto_tools
    for chat_only in {
        "list_new_feedback_emails",
        "list_unread_inbox_emails",
        "list_unread_project_emails",
        "search_legacy_reply_templates",
        "get_support_knowledge_summary",
        "get_support_coverage_summary",
        "get_case_state",
        "write_audit_log",
    }:
        assert chat_only in chat_tools
        assert chat_only not in auto_tools


def test_auto_tools_block_restart_tools_after_decision():
    tools = workflows.build_multi_project_workflow(SupportAgentConfig()).tools

    decision = tools["decide_support_action"].callable(
        case_type="general_question",
        verdict="supported",
        confidence=0.95,
        risk_level="low",
        applied_rule_ids=["delete_account_data_no_account"],
        rule_action="draft_reply",
        rule_human_review=False,
    )
    blocked = tools["read_email_thread"].callable(thread_id="19f222614b39d78b")

    assert decision["action"] == "create_draft"
    assert blocked["blocked"] is True
    assert blocked["tool"] == "read_email_thread"
    assert "decide_support_action already returned" in blocked["error"]
    assert "save_case_state" in blocked["next_steps"]


def test_cleanup_workflow_only_exposes_gmail_finish_tools():
    workflow = workflows.build_multi_project_workflow(
        SupportAgentConfig(),
        surface="cleanup",
    )

    assert set(workflow.tools) == {
        "get_existing_gmail_labels",
        "apply_existing_gmail_labels",
        "mark_gmail_messages_read",
        "save_case_state",
    }
    assert workflow.required_steps == []
    assert workflow.terminal_tool == "save_case_state"


def test_auto_task_is_compact_and_keeps_required_case_data():
    task = build_auto_task(
        [
            {
                "message_id": "m1",
                "thread_id": "t1",
                "project_label": "BlackHole",
                "matched_labels": ["BlackHole", "BlackHole/bug反馈"],
            }
        ],
        live_run=False,
    )

    assert len(task) < 2500
    assert "message_id=m1" in task
    assert "thread_id=t1" in task
    assert "project_label=BlackHole" in task
    assert "save_case_state" in task
    assert "Support workflow details live in the system prompt" in task
    assert "BlackHole 关卡内目标物品找不到" not in task
    assert "get_reply_template 每个 template_id 最多调用一次" not in task


def test_auto_task_reprocess_cleanup_is_minimal_and_preserves_existing_draft():
    task = build_auto_task(
        [
            {
                "message_id": "m2",
                "thread_id": "t2",
                "project_label": "BusFever",
                "matched_labels": ["BusFever"],
                "reprocess_gmail_unread": True,
                "existing_status": "draft_created",
                "existing_draft_id": "draft-2",
                "existing_issue_type": "save_transfer",
                "existing_recommended_labels": ["BusFever", "BusFever/存档转移"],
            }
        ],
        live_run=True,
    )

    assert len(task) < 2200
    assert "existing_draft_id=draft-2" in task
    assert "cleanup-only" in task
    assert "do not call create_gmail_draft" in task
    assert "do not call read_email_thread" in task


def test_chat_server_readiness_buttons_have_chinese_tooltips():
    page = chat_server.PAGE

    assert 'data-tooltip="只读取本地配置并输出安全摘要，不连接模型或 Gmail。"' in page
    assert 'data-tooltip="检查模型服务、Gmail 凭据、ClickHouse 和状态目录是否可用。"' in page
    assert 'data-tooltip="在 Readiness 基础上额外扫描 Gmail 未读项目候选，不调用模型。"' in page


def test_chat_server_recent_runs_render_as_brief_summaries():
    page = chat_server.PAGE

    assert '<pre id="runs">' not in page
    assert 'id="runs" class="run-list"' in page
    assert "function renderRunSummary" in page
    assert "JSON.stringify(data.runs" not in page
    assert "input_preview" in page
    assert "answer_preview" in page


def test_chat_server_quick_buttons_run_agent_directly():
    page = chat_server.PAGE

    assert "async function runChat(message" in page
    assert "runChat(button.dataset.prompt)" in page
    assert "input.value = button.dataset.prompt" not in page
    assert 'id="quick-live-one"' in page
    assert "runManual({" in page
    assert "max_new: 1" in page
    assert "requestManualStream" in page
    assert "/api/manual-run/stream" in page
    assert "cursor: not-allowed" in page
    assert "setProgressLine" in page
    assert "formatManualDone" in page
    assert "human_summary" in page
    assert 'id="sender-email"' in page
    assert 'id="manual-sender-live"' in page
    assert "按发件人正式处理" in page
    assert "sender_email" in page


def test_resolve_manual_discovery_query_adds_sender_filter():
    config = SupportAgentConfig()
    query, sender = chat_server.resolve_manual_discovery_query(
        config,
        {"sender_email": "Player@Example.com"},
    )

    assert sender == "player@example.com"
    assert query.endswith("from:player@example.com")
    assert config.gmail.feedback_query in query


def test_resolve_manual_discovery_query_merges_explicit_query_and_sender():
    config = SupportAgentConfig()
    query, sender = chat_server.resolve_manual_discovery_query(
        config,
        {
            "query": 'label:"BlackHole"',
            "sender_email": "player@example.com",
        },
    )

    assert sender == "player@example.com"
    assert query == 'label:"BlackHole" from:player@example.com'


def test_resolve_manual_discovery_query_rejects_invalid_sender():
    with pytest.raises(chat_server.BadRequest, match="发件人邮箱格式无效"):
        chat_server.resolve_manual_discovery_query(
            SupportAgentConfig(),
            {"sender_email": "not-an-email"},
        )


@pytest.mark.asyncio
async def test_chat_server_manual_run_passes_sender_query(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    async def fake_run_once(**kwargs):
        captured.update(kwargs)
        kwargs["status_sink"]("[完成] 无新邮件")
        return {
            "run_id": "auto-1",
            "status": "skipped",
            "candidate_count": 0,
            "selected_count": 0,
        }

    monkeypatch.setattr(chat_server, "run_once", fake_run_once)
    config = SupportAgentConfig(
        state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
    )
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=config,
        manual_store_path=str(tmp_path / "manual.json"),
    )

    await chat_server.run_manual_trigger(
        control,
        {
            "sender_email": "player@example.com",
            "live": False,
            "discovery_only": True,
        },
    )

    assert "from:player@example.com" in str(captured["query"])
    assert config.gmail.feedback_query in str(captured["query"])


def test_chat_server_streams_chat_statuses_to_dialog():
    page = chat_server.PAGE
    source = inspect.getsource(chat_server.ChatHandler)

    assert "fetch('/api/chat/stream'" in page
    assert "new TextDecoder()" in page
    assert "event.type === 'status'" in page
    assert "requestJson('/api/chat'" not in page
    assert 'self.path == "/api/chat/stream"' in source
    assert 'emit({"type": "status"' in source


def test_parse_web_ui_local_config_reads_cloud_settings(tmp_path):
    config_path = tmp_path / "web-ui.config.local.sh"
    config_path.write_text(
        '\n'.join(
            [
                'CLOUD_API_KEY="secret-key"',
                'CLOUD_MODEL="deepseek-v4-pro"',
                'CLOUD_BASE_URL="https://api.deepseek.com"',
            ]
        ),
        encoding="utf-8",
    )

    parsed = chat_server._parse_web_ui_local_config(config_path)

    assert parsed["CLOUD_API_KEY"] == "secret-key"
    assert parsed["CLOUD_MODEL"] == "deepseek-v4-pro"
    assert parsed["CLOUD_BASE_URL"] == "https://api.deepseek.com"


def test_parse_web_ui_local_config_reads_local_model_settings(tmp_path):
    config_path = tmp_path / "web-ui.config.local.sh"
    config_path.write_text(
        '\n'.join(
            [
                'LLAMA_SERVER_BIN="/tmp/llama-server"',
                'GGUF_PATH="/models/support.gguf"',
                'LLAMA_HOST="127.0.0.1"',
                'LLAMA_PORT="8088"',
                'LLAMA_NGL="888"',
            ]
        ),
        encoding="utf-8",
    )

    parsed = chat_server._parse_web_ui_local_config(config_path)

    assert parsed["LLAMA_SERVER_BIN"] == "/tmp/llama-server"
    assert parsed["GGUF_PATH"] == "/models/support.gguf"
    assert parsed["LLAMA_HOST"] == "127.0.0.1"
    assert parsed["LLAMA_PORT"] == "8088"
    assert parsed["LLAMA_NGL"] == "888"


def test_bootstrap_cloud_env_reads_startup_key_and_legacy_path(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    legacy_dir = tmp_path / "cloud_model_keys"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "startup.key").write_text("legacy-key", encoding="utf-8")
    monkeypatch.setattr(chat_server, "DEFAULT_CLOUD_KEY_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(chat_server, "_LEGACY_CLOUD_KEY_DIR", str(legacy_dir))

    chat_server._bootstrap_cloud_env_from_local_files()

    assert os.environ["OPENAI_API_KEY"] == "legacy-key"


def test_model_profile_run_config_normalizes_misplaced_api_key_env(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    base = AgentRunConfig(config_path="config.toml")
    model = ModelConfig(
        backend="openai-compatible",
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        api_key_env="sk-test-key",
    )

    resolved = chat_server._model_profile_run_config(base, model)

    assert resolved.api_key == "sk-test-key"
    assert resolved.api_key_env == "OPENAI_API_KEY"


def test_chat_server_can_switch_named_model_profiles(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORT_MODEL_API_KEY", "model-key")
    config = SupportAgentConfig(
        model=ModelConfig(
            backend="llamaserver",
            gguf_path="/models/local.gguf",
            base_url="http://localhost:8080/v1",
        ),
        model_profiles={
            "cloud": ModelConfig(
                backend="openai-compatible",
                model="support-cloud",
                base_url="https://provider.example/v1",
                api_key_env="SUPPORT_MODEL_API_KEY",
            )
        },
        state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
    )
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=config,
    )

    status = control.select_model_profile("cloud")

    assert status["active_profile"] == "cloud"
    assert status["model"]["backend"] == "openai-compatible"
    assert status["model"]["model"] == "support-cloud"
    assert status["model"]["base_url"] == "https://provider.example/v1"
    assert status["model"]["credential_configured"] is True
    assert "model-key" not in json.dumps(status, ensure_ascii=False)
    assert "api_key" not in json.dumps(status, ensure_ascii=False)
    assert control.runner.run_config.backend == "openai-compatible"


def test_local_model_manager_starts_and_stops_owned_llama_server(tmp_path):
    server_bin = tmp_path / "llama-server"
    server_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    server_bin.chmod(0o755)
    gguf = tmp_path / "support.gguf"
    gguf.write_text("model", encoding="utf-8")
    process_events: list[str] = []

    class FakeProcess:
        pid = 4242

        def __init__(self) -> None:
            self.stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            process_events.append("terminate")
            self.stopped = True

        def wait(self, timeout=None):
            process_events.append(f"wait:{timeout}")
            return 0

    popen_args: list[list[str]] = []

    def fake_popen(args, **_kwargs):
        popen_args.append(list(args))
        return FakeProcess()

    health_results = iter([False, True, False])
    manager = chat_server.LocalModelServiceManager(
        chat_server.LocalModelLaunchConfig(
            server_bin=str(server_bin),
            gguf_path=str(gguf),
            host="127.0.0.1",
            port=8088,
            ngl="888",
            log_path=tmp_path / "llama.log",
        ),
        popen_factory=fake_popen,
        health_checker=lambda _base_url: next(health_results),
        sleeper=lambda _seconds: None,
    )

    started = manager.start(timeout_seconds=1)

    assert started["state"] == "running"
    assert started["owned"] is True
    assert started["pid"] == 4242
    assert started["base_url"] == "http://127.0.0.1:8088/v1"
    assert popen_args[0][:3] == [str(server_bin), "-m", str(gguf)]
    assert "--jinja" in popen_args[0]
    assert "-ngl" in popen_args[0]

    stopped = manager.stop()

    assert stopped["state"] == "stopped"
    assert stopped["owned"] is False
    assert process_events == ["terminate", "wait:5"]


def test_local_model_manager_does_not_claim_or_stop_external_service(tmp_path):
    server_bin = tmp_path / "llama-server"
    server_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    server_bin.chmod(0o755)
    gguf = tmp_path / "support.gguf"
    gguf.write_text("model", encoding="utf-8")
    popen_calls: list[list[str]] = []
    manager = chat_server.LocalModelServiceManager(
        chat_server.LocalModelLaunchConfig(
            server_bin=str(server_bin),
            gguf_path=str(gguf),
            host="127.0.0.1",
            port=8088,
            log_path=tmp_path / "llama.log",
        ),
        popen_factory=lambda args, **_kwargs: popen_calls.append(list(args)),
        health_checker=lambda _base_url: True,
        sleeper=lambda _seconds: None,
    )

    started = manager.start(timeout_seconds=1)
    stopped = manager.stop()

    assert started["state"] == "external"
    assert started["owned"] is False
    assert stopped["state"] == "external"
    assert stopped["owned"] is False
    assert popen_calls == []


def test_chat_server_select_local_profile_starts_local_model_service(tmp_path):
    class FakeLocalModelManager:
        def __init__(self) -> None:
            self.started_with: AgentRunConfig | None = None

        def status(self):
            return {"state": "stopped", "owned": False}

        def start(self, run_config, *, timeout_seconds=120):
            self.started_with = run_config
            return {
                "state": "running",
                "owned": True,
                "base_url": run_config.base_url,
                "pid": 123,
            }

        def stop(self):
            return {"state": "stopped", "owned": False}

    fake_local = FakeLocalModelManager()
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=SupportAgentConfig(
            model=ModelConfig(
                backend="llamaserver",
                gguf_path="/models/local.gguf",
                base_url="http://127.0.0.1:8088/v1",
            ),
            state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        ),
        local_model_manager=fake_local,
    )

    status = control.select_model_profile("local")

    assert status["active_profile"] == "local"
    assert status["model"]["backend"] == "llamaserver"
    assert status["local_model"]["state"] == "running"
    assert fake_local.started_with is not None
    assert fake_local.started_with.gguf_path == "/models/local.gguf"
    assert fake_local.started_with.base_url == "http://127.0.0.1:8088/v1"


def test_chat_server_can_configure_missing_cloud_profile_from_ui(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SUPPORT_MODEL_API_KEY", "model-key")
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=SupportAgentConfig(
            model=ModelConfig(
                backend="llamaserver",
                gguf_path="/models/local.gguf",
                base_url="http://localhost:8080/v1",
            ),
            state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        ),
    )

    before = control.status()
    cloud_before = next(
        profile for profile in before["profiles"] if profile["name"] == "cloud"
    )
    assert cloud_before["configured"] is False

    status = control.select_model_profile(
        "cloud",
        profile_config={
            "model": "support-cloud",
            "base_url": "https://provider.example/v1",
            "api_key_env": "SUPPORT_MODEL_API_KEY",
        },
    )

    cloud_after = next(
        profile for profile in status["profiles"] if profile["name"] == "cloud"
    )
    assert cloud_after["configured"] is True
    assert status["active_profile"] == "cloud"
    assert status["model"]["backend"] == "openai-compatible"
    assert status["model"]["model"] == "support-cloud"
    assert status["model"]["credential_configured"] is True
    assert control.runner.run_config.backend == "openai-compatible"


def test_chat_server_rejects_plain_cloud_api_key_from_ui(tmp_path):
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=SupportAgentConfig(
            state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        ),
    )

    with pytest.raises(chat_server.BadRequest, match="api_key_env"):
        control.select_model_profile(
            "cloud",
            profile_config={
                "model": "support-cloud",
                "base_url": "https://provider.example/v1",
                "api_key": "secret-value",
            },
        )


def test_chat_server_can_save_and_switch_cloud_api_key_without_echoing_secret(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("UNUSED_MODEL_KEY", "unused")
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=SupportAgentConfig(
            state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        ),
        cloud_key_dir=str(tmp_path / "cloud_keys"),
    )

    status = control.save_cloud_api_key(
        {
            "name": "primary",
            "api_key": "secret-model-key",
            "model": "support-cloud",
            "base_url": "https://provider.example/v1",
        }
    )

    assert status["active_profile"] == "cloud"
    assert status["active_cloud_key"] == "primary"
    assert status["cloud_keys"] == [{"name": "primary", "active": True}]
    assert status["model"]["backend"] == "openai-compatible"
    assert status["model"]["model"] == "support-cloud"
    assert status["model"]["credential_configured"] is True
    assert control.runner.run_config.api_key_file
    assert "secret-model-key" not in json.dumps(status, ensure_ascii=False)
    assert control.cloud_key_store.key_path("primary").read_text(
        encoding="utf-8"
    ) == "secret-model-key"

    switched = control.save_cloud_api_key(
        {
            "name": "primary",
            "model": "support-cloud",
            "base_url": "https://provider.example/v1",
        }
    )

    assert switched["active_cloud_key"] == "primary"
    assert switched["model"]["credential_configured"] is True


def test_chat_server_rejects_unknown_saved_cloud_api_key(tmp_path):
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=SupportAgentConfig(
            state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        ),
        cloud_key_dir=str(tmp_path / "cloud_keys"),
    )

    with pytest.raises(chat_server.BadRequest, match="No saved cloud API key"):
        control.save_cloud_api_key(
            {
                "name": "missing",
                "model": "support-cloud",
                "base_url": "https://provider.example/v1",
            }
        )


@pytest.mark.asyncio
async def test_chat_server_manual_run_uses_auto_worker_run_once(
    monkeypatch,
    tmp_path,
):
    captured: dict[str, object] = {}

    async def fake_run_once(**kwargs):
        captured.update(kwargs)
        kwargs["status_sink"]("[完成] 已检测候选邮件：1")
        return {
            "run_id": "auto-1",
            "status": "discovery_only",
            "candidate_count": 1,
            "selected_count": 0,
        }

    monkeypatch.setattr(chat_server, "run_once", fake_run_once)
    config = SupportAgentConfig(
        state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
    )
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=config,
        manual_store_path=str(tmp_path / "manual.json"),
    )

    payload = {
        "max_candidates": 7,
        "max_new": 2,
        "max_retries": 4,
        "retry_failed": True,
        "ignore_store": True,
        "discovery_only": True,
        "query": 'label:"BlackHole"',
    }
    result = await chat_server.run_manual_trigger(control, payload)

    assert result["result"]["status"] == "discovery_only"
    assert result["statuses"] == ["[完成] 已检测候选邮件：1"]
    assert captured["support_config"] is config
    assert captured["max_candidates"] == 7
    assert captured["max_new"] == 2
    assert captured["max_retries"] == 4
    assert captured["retry_failed"] is True
    assert captured["ignore_store"] is True
    assert captured["discovery_only"] is True
    assert captured["query"] == 'label:"BlackHole"'
    assert captured["live_run"] is False
    assert captured["store"].path == tmp_path / "manual.json"


@pytest.mark.asyncio
async def test_chat_server_manual_run_stream_emits_status_and_done(
    monkeypatch,
    tmp_path,
):
    async def fake_run_once(**kwargs):
        kwargs["status_sink"]("[完成] 已检测候选邮件：1")
        return {
            "run_id": "auto-1",
            "status": "discovery_only",
            "candidate_count": 1,
            "selected_count": 0,
        }

    monkeypatch.setattr(chat_server, "run_once", fake_run_once)
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=SupportAgentConfig(
            state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        ),
        manual_store_path=str(tmp_path / "manual.json"),
    )
    events: list[dict[str, object]] = []

    await chat_server.run_manual_trigger(
        control,
        {"discovery_only": True, "max_candidates": 3},
        stream_emit=events.append,
    )

    assert events[0]["type"] == "status"
    assert "手动发现邮件" in str(events[0]["message"])
    assert any(
        event.get("type") == "status" and "[完成]" in str(event.get("message"))
        for event in events
    )
    assert events[-1]["type"] == "done"
    assert events[-1]["result"]["status"] == "discovery_only"


@pytest.mark.asyncio
async def test_chat_server_manual_live_run_requires_confirmation(tmp_path):
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=SupportAgentConfig(
            state=StateConfig(processed_store_path=str(tmp_path / "processed.json"))
        ),
        manual_store_path=str(tmp_path / "manual.json"),
    )

    with pytest.raises(chat_server.BadRequest, match="确认正式处理"):
        await chat_server.run_manual_trigger(control, {"live": True})


def test_chat_server_page_has_automation_controls():
    page = chat_server.PAGE
    source = inspect.getsource(chat_server.ChatHandler)

    assert "<h2>自动处理</h2>" in page
    assert 'id="auto-start-dry"' in page
    assert 'id="auto-start-live"' in page
    assert 'id="auto-stop"' in page
    assert "automation-session-banner" in page
    assert "pollAutomationFeed" in page
    assert "/api/automation/feed" in page
    assert "requestJson('/api/automation/start'" in page
    assert 'self.path == "/api/automation/start"' in source
    assert 'self.path == "/api/manual-run/stream"' in source
    assert 'self.path == "/api/automation/stop"' in source
    assert 'parsed.path == "/api/automation/feed"' in source


def test_chat_server_page_has_local_model_controls():
    page = chat_server.PAGE
    source = inspect.getsource(chat_server.ChatHandler)

    assert 'id="local-model-stop"' in page
    assert "renderLocalModelStatus" in page
    assert "/api/local-model/start" in page
    assert "/api/local-model/stop" in page
    assert "关闭本地模型" in page
    assert 'self.path == "/api/local-model/start"' in source
    assert 'self.path == "/api/local-model/stop"' in source


def test_automation_feed_tracks_session_cycles(tmp_path):
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=SupportAgentConfig(
            state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        ),
        manual_store_path=str(tmp_path / "manual.json"),
    )
    control.automation.running = True
    control.automation.session_id = "auto-session-feed"
    control.automation.started_at = "2026-06-15T10:00:00+00:00"
    control.automation.settings = chat_server.AutomationSettings(
        interval_seconds=300,
        max_candidates=20,
        max_new=1,
        max_retries=3,
        retry_failed=False,
        live_run=False,
        query=None,
    )
    control.automation.session_cycles = [
        chat_server.CycleSnapshot(
            run_id="r1",
            created_at="2026-06-15T10:01:00+00:00",
            status="draft_created",
            selected_count=1,
            headline="已创建草稿",
            text="本轮结果：已创建草稿",
        )
    ]

    feed = chat_server.automation_feed(control)
    assert feed["session_id"] == "auto-session-feed"
    assert feed["summary"]["processed_count"] == 1
    assert feed["events"][0]["type"] == "session_started"
    assert feed["events"][-1]["run_id"] == "r1"

    delta = chat_server.automation_feed(control, after_run_id="r1")
    assert delta["events"] == []


@pytest.mark.asyncio
async def test_chat_server_automation_uses_config_store_and_run_once(
    monkeypatch,
    tmp_path,
):
    captured: dict[str, object] = {}

    async def fake_run_once(**kwargs):
        captured.update(kwargs)
        return {
            "run_id": "auto-loop-1",
            "status": "skipped",
            "candidate_count": 0,
            "selected_count": 0,
        }

    monkeypatch.setattr(chat_server, "run_once", fake_run_once)
    config = SupportAgentConfig(
        state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
    )
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=config,
        manual_store_path=str(tmp_path / "manual.json"),
    )
    control.automation.settings = chat_server.AutomationSettings(
        interval_seconds=300,
        max_candidates=20,
        max_new=5,
        max_retries=3,
        retry_failed=False,
        live_run=False,
        query=None,
    )
    control.automation.session_id = "auto-session-test"

    result = await control.automation._run_cycle()

    assert result["status"] == "skipped"
    assert captured["store"].path == tmp_path / "processed.json"
    assert captured["ignore_store"] is False
    assert captured["discovery_only"] is False
    assert captured["max_new"] == 5
    assert captured["run_source"] == "automation"
    assert captured["automation_session_id"] == "auto-session-test"


@pytest.mark.asyncio
async def test_chat_server_automation_clears_live_store_only_on_first_cycle(
    monkeypatch,
    tmp_path,
):
    captured: list[dict[str, object]] = []

    async def fake_run_once(**kwargs):
        captured.append(dict(kwargs))
        return {
            "run_id": f"auto-loop-{len(captured)}",
            "status": "failed",
            "candidate_count": 1,
            "selected_count": 1,
        }

    monkeypatch.setattr(chat_server, "run_once", fake_run_once)
    config = SupportAgentConfig(
        state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
    )
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=config,
        manual_store_path=str(tmp_path / "manual.json"),
    )
    control.automation.settings = chat_server.AutomationSettings(
        interval_seconds=300,
        max_candidates=20,
        max_new=1,
        max_retries=3,
        retry_failed=False,
        live_run=True,
        query=None,
    )
    control.automation.session_id = "auto-session-test"

    await control.automation._run_cycle()
    control.automation.cycle_count = 1
    await control.automation._run_cycle()

    assert captured[0]["clear_store_state"] is True
    assert captured[1]["clear_store_state"] is False


def test_chat_server_automation_start_stop(monkeypatch, tmp_path):
    async def fake_run_once(**_kwargs):
        return {
            "run_id": "auto-loop-2",
            "status": "skipped",
            "candidate_count": 0,
            "selected_count": 0,
        }

    monkeypatch.setattr(chat_server, "run_once", fake_run_once)
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=SupportAgentConfig(
            state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        ),
        manual_store_path=str(tmp_path / "manual.json"),
    )

    started = chat_server.start_automation(
        control,
        {
            "interval_seconds": 60,
            "max_new": 2,
            "live": False,
        },
    )
    assert started["running"] is True
    assert started["interval_seconds"] == 60
    assert started["max_new"] == 2
    assert control.status()["automation"]["running"] is True

    stopped = chat_server.stop_automation(control)
    assert stopped["running"] is False


def test_chat_server_automation_defaults_to_single_message(tmp_path):
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=SupportAgentConfig(
            state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        ),
        manual_store_path=str(tmp_path / "manual.json"),
    )

    started = chat_server.start_automation(
        control,
        {
            "interval_seconds": 60,
            "live": False,
        },
    )

    assert started["max_new"] == 1


def test_chat_server_automation_live_requires_confirmation(tmp_path):
    control = chat_server.ControlState(
        AgentRunConfig(config_path="config.toml"),
        support_config=SupportAgentConfig(
            state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        ),
        manual_store_path=str(tmp_path / "manual.json"),
    )

    with pytest.raises(chat_server.BadRequest, match="确认正式处理"):
        chat_server.start_automation(control, {"live": True, "interval_seconds": 60})


def test_manual_trigger_uses_separate_default_store():
    source = inspect.getsource(manual_trigger)

    assert "manual_trigger_processed_messages.json" in source
    assert "run_once" in source
    assert "GmailTools(" not in source


def test_agent_user_message_keeps_model_in_control():
    message = build_user_message(
        "查看 NumberCrush 标签中有多少封邮件",
        live_run=False,
        allow_db_in_dry_run=True,
        memory=ChatMemory(),
    )

    assert "DRY-RUN" in message
    assert "User question:" in message
    assert "查看 NumberCrush 标签中有多少封邮件" in message
    assert "call whatever tools you need" in message


def test_chat_memory_renders_recent_mailbox_references():
    memory = ChatMemory()
    memory.remember_tool_result(
        Message(
            MessageRole.TOOL,
            json.dumps(
                {
                    "query": "is:unread",
                    "messages": [
                        {
                            "message_id": "m1",
                            "thread_id": "t1",
                            "subject": "Purchase issue",
                            "from": "player@example.com",
                            "date": "Mon, 1 Jun 2026 10:00:00 +0000",
                            "snippet": "I paid but did not receive it",
                            "project_labels": ["NumberCrush"],
                            "label_names": ["NumberCrush", "NumberCrush/内购问题"],
                        }
                    ],
                }
            ),
            MessageMeta(MessageType.TOOL_RESULT),
            tool_name="list_unread_inbox_emails",
            tool_call_id="call_1",
        )
    )

    rendered = memory.render()

    assert "Recent mailbox references" in rendered
    assert "#1 | thread_id=t1 | message_id=m1" in rendered
    assert "subject=Purchase issue" in rendered
    assert "project=NumberCrush" in rendered
    assert "snippet=" not in rendered
    assert "labels=" not in rendered
    assert "do not process the email" in rendered.lower()


def test_chat_memory_limits_mailbox_reference_context_size():
    memory = ChatMemory()
    memory.remember_tool_result(
        Message(
            MessageRole.TOOL,
            json.dumps(
                {
                    "query": "is:unread in:inbox -in:spam -in:trash",
                    "messages": [
                        {
                            "message_id": f"m{i}",
                            "thread_id": f"t{i}",
                            "subject": "x" * 200,
                            "from": "very-long-player-address@example.com",
                            "date": "Mon, 1 Jun 2026 10:00:00 +0000",
                            "snippet": "y" * 500,
                            "project_labels": ["NumberCrush"],
                            "label_names": [
                                "NumberCrush",
                                "NumberCrush/内购问题",
                                "INBOX",
                                "UNREAD",
                            ],
                        }
                        for i in range(1, 21)
                    ],
                }
            ),
            MessageMeta(MessageType.TOOL_RESULT),
            tool_name="list_unread_inbox_emails",
            tool_call_id="call_1",
        )
    )

    rendered = memory.render_mailbox_refs()

    assert "#10 |" in rendered
    assert "#11 |" not in rendered
    assert "snippet=" not in rendered
    assert "labels=" not in rendered
    assert len(rendered) < 2200


def test_message_observer_stores_mailbox_references_in_memory():
    memory = ChatMemory()
    observer = build_message_observer(
        status_sink=lambda _: None,
        case_states=[],
        memory=memory,
    )

    observer(
        Message(
            MessageRole.TOOL,
            json.dumps(
                {
                    "messages": [
                        {
                            "message_id": "m1",
                            "thread_id": "t1",
                            "project_label": "BlackHole",
                            "matched_labels": ["BlackHole/bug反馈"],
                        }
                    ]
                }
            ),
            MessageMeta(MessageType.TOOL_RESULT),
            tool_name="list_unread_project_emails",
            tool_call_id="call_1",
        )
    )

    assert memory.mailbox_refs[0]["thread_id"] == "t1"
    assert memory.mailbox_refs[0]["project"] == "BlackHole"


def test_agent_user_message_includes_recent_mailbox_references():
    memory = ChatMemory()
    memory.mailbox_refs = [
        {
            "index": "1",
            "thread_id": "t1",
            "message_id": "m1",
            "subject": "Purchase issue",
        }
    ]
    memory.mailbox_source = "list_unread_inbox_emails"

    message = build_user_message(
        "序号1是哪一封邮件？",
        live_run=False,
        allow_db_in_dry_run=True,
        memory=memory,
    )

    assert "Recent mailbox references" in message
    assert "#1 | thread_id=t1 | message_id=m1 | subject=Purchase issue" in message
    assert "do not process" in message.lower()


def test_interactive_prompt_does_not_claim_runtime_is_local():
    prompt = prompts.MULTI_PROJECT_INTERACTIVE_CHAT_PROMPT

    assert "with a local model" not in prompt
    assert "currently selected model runtime" in prompt
    assert "Recent mailbox references" in prompt
    assert "which email an ordinal refers to" in prompt
    assert "sender/subject/project metadata" in prompt


@pytest.mark.asyncio
async def test_support_runner_injects_current_model_runtime_context(monkeypatch):
    captured = {}

    class FakeWorkflowRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, workflow, user_message):
            captured["workflow_description"] = workflow.description
            captured["user_message"] = user_message
            return "ok"

    monkeypatch.setattr(agent_runner, "WorkflowRunner", FakeWorkflowRunner)

    runner = agent_runner.SupportAgentRunner(
        AgentRunConfig(
            backend="openai-compatible",
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key="super-secret",
        ),
        support_config=SupportAgentConfig(),
    )

    result = await runner.run("你是什么模型", status_sink=lambda _: None)

    assert result.answer == "ok"
    user_message = captured["user_message"]
    assert "Current model runtime:" in user_message
    assert "backend: openai-compatible" in user_message
    assert "model: deepseek-v4-pro" in user_message
    assert "base_url: https://api.deepseek.com" in user_message
    assert "super-secret" not in user_message
    assert "api_key" not in user_message


def test_agent_run_config_uses_named_model_profile(monkeypatch):
    monkeypatch.setenv("SUPPORT_MODEL_API_KEY", "model-key")
    support_config = SupportAgentConfig(
        model=ModelConfig(
            backend="llamaserver",
            gguf_path="/models/local.gguf",
            base_url="http://localhost:8080/v1",
        ),
        model_profiles={
            "cloud": ModelConfig(
                backend="openai-compatible",
                model="support-cloud",
                base_url="https://provider.example/v1",
                api_key_env="SUPPORT_MODEL_API_KEY",
            )
        },
    )
    runner = agent_runner.SupportAgentRunner(
        AgentRunConfig(profile="cloud"),
        support_config=support_config,
    )

    assert runner.run_config.backend == "openai-compatible"
    assert runner.run_config.model == "support-cloud"
    assert runner.run_config.base_url == "https://provider.example/v1"
    assert run_config_has_api_key(runner.run_config) is True


def test_agent_run_config_merges_cloud_model_config(monkeypatch):
    monkeypatch.setenv("SUPPORT_MODEL_API_KEY", "model-key")

    resolved = resolve_run_config(
        AgentRunConfig(),
        ModelConfig(
            backend="openai-compatible",
            model="support-model",
            base_url="https://provider.example/v1",
            api_key_env="SUPPORT_MODEL_API_KEY",
            timeout_seconds=30,
            budget_tokens=2048,
            max_iterations=9,
        ),
    )

    assert resolved.backend == "openai-compatible"
    assert resolved.model == "support-model"
    assert resolved.base_url == "https://provider.example/v1"
    assert resolved.timeout_seconds == 30
    assert resolved.budget_tokens == 2048
    assert resolved.max_iterations == 9
    assert run_config_has_api_key(resolved) is True


def test_auto_task_builder_only_lists_new_message_targets():
    task = build_auto_task(
        [
            {"message_id": "m1", "thread_id": "t1"},
            {"message_id": "m2", "thread_id": "t2"},
        ],
        live_run=False,
    )

    assert "message_id=m1 thread_id=t1" in task
    assert "message_id=m2 thread_id=t2" in task
    assert len(task) < 2500
    assert "Support workflow details live in the system prompt" in task
    assert "save_case_state" in task
    assert "Allowed final statuses: draft_created, human_review, failed, skipped" in task
    assert "My question is:" not in task


def test_auto_task_builder_places_labels_after_decision():
    task = build_auto_task(
        [{"message_id": "m1", "thread_id": "t1"}],
        live_run=False,
    )

    assert "When applying labels" in task
    assert "exact recommended_labels" in task
    assert "Support workflow details live in the system prompt" in task
    assert "review_reply_draft" not in task


def test_auto_task_builder_documents_ads_after_purchase_label_priority():
    task = build_auto_task([{"message_id": "m1", "thread_id": "t1"}], live_run=False)

    assert "去广告后有广告" in prompts.MULTI_PROJECT_SUPPORT_PROMPT
    assert "内购问题" in prompts.MULTI_PROJECT_SUPPORT_PROMPT
    assert "recommended_labels" in task
    assert "去广告后有广告" not in task


def test_auto_task_builder_documents_ads_after_purchase_clickhouse_flow():
    task = build_auto_task([{"message_id": "m1", "thread_id": "t1"}], live_run=False)

    prompt = prompts.MULTI_PROJECT_SUPPORT_PROMPT
    assert "ads_after_purchase" in prompt
    assert "get_remove_ads_investigation_playbook" in prompt
    assert "assess_remove_ads_log_evidence" in prompt
    assert "ads_after_purchase" not in task


def test_prompt_documents_ads_after_purchase_workflow():
    prompt = prompts.MULTI_PROJECT_SUPPORT_PROMPT

    assert "Ads after purchase workflow" in prompt
    assert "get_remove_ads_investigation_playbook" in prompt
    assert "assess_remove_ads_log_evidence" in prompt


def test_auto_task_builder_skips_clickhouse_when_evidence_unavailable():
    task = build_auto_task([{"message_id": "m1", "thread_id": "t1"}], live_run=False)

    prompt = prompts.MULTI_PROJECT_SUPPORT_PROMPT
    assert "available=false" not in task
    assert "skip_clickhouse_fallback=true" in prompt
    assert "ad_issue" in prompt
    assert "ad_redirect_reset_ad_id" in prompt
    assert "ad_loading_playback_troubleshooting" in prompt
    assert "language_fallback=true" in prompt
    assert "has_strong_match=false" in prompt


def test_prompt_skips_clickhouse_when_evidence_unavailable():
    prompt = prompts.MULTI_PROJECT_SUPPORT_PROMPT

    assert "skip_clickhouse_fallback=true" in prompt
    assert "Anti-loop rules" in prompt
    assert "get_relevant_support_rules once" in prompt
    assert "has_strong_match=false" in prompt
    assert "language_fallback=true" in prompt
    assert "vague_issue_details_request" in prompt or "decide_support_action" in prompt or "immediately decide" in prompt.lower()
    assert "Ad issue workflow" in prompt
    assert "Never query ClickHouse for ad_issue" in prompt


def test_create_draft_prompt_mentions_review_before_draft():
    prompt = prompts.MULTI_PROJECT_SUPPORT_PROMPT

    assert "review_reply_draft" in prompt
    assert prompt.index("review_reply_draft") < prompt.index("create_gmail_draft")
    # Updated order: apply labels (side effect) then final save_case_state
    assert "apply_existing_gmail_labels" in prompt
    assert "save_case_state" in prompt
    assert "save_case_state" in prompt and ("final" in prompt or "最终" in prompt)


def test_auto_task_builder_saves_case_state_before_labels():
    task = build_auto_task([{"message_id": "m1", "thread_id": "t1"}], live_run=False)

    assert task.index("mark_gmail_messages_read") < task.index("save_case_state")
    assert "exact recommended_labels" in task
    assert "save_case_state" in task
    assert "create_gmail_draft 成功后" not in task


def test_prompt_and_auto_task_document_no_content_label_flow():
    prompt = prompts.MULTI_PROJECT_SUPPORT_PROMPT
    task = build_auto_task([{"message_id": "m1", "thread_id": "t1"}], live_run=False)

    assert "no_content" in prompt
    assert "无内容" in prompt
    assert "mark_gmail_messages_read" in prompt
    assert "mark_gmail_messages_read" in task
    assert "case_type=no_content" not in task
    assert "save_case_state(status=skipped)" not in task
    assert "Tools do not" in prompt or "mark_gmail_messages_read" in prompt or "no_content" in prompt


def test_prompt_and_auto_task_align_no_content_with_prerequisites():
    prompt = prompts.MULTI_PROJECT_SUPPORT_PROMPT
    task = build_auto_task([{"message_id": "m1", "thread_id": "t1"}], live_run=False)

    prompt_section = prompt.split("No-content emails (case_type=no_content):", 1)[1].split(
        "Player identity rules:",
        1,
    )[0]

    assert "resolve_player_identity" in prompt_section
    assert "assess_claim_credibility" in prompt_section
    assert "decide_support_action" in prompt_section
    assert prompt_section.index("resolve_player_identity") < prompt_section.index(
        "assess_claim_credibility"
    )
    assert prompt_section.index("assess_claim_credibility") < prompt_section.index(
        "decide_support_action"
    )
    assert "Support workflow details live in the system prompt" in task
    assert "resolve_player_identity" not in task


def test_prompt_and_auto_task_document_thread_latest_player_reply_policy():
    prompt = prompts.MULTI_PROJECT_SUPPORT_PROMPT
    chat_prompt = prompts.MULTI_PROJECT_INTERACTIVE_CHAT_PROMPT
    task = build_auto_task([{"message_id": "m1", "thread_id": "t1"}], live_run=False)

    assert "latest player-authored inbound message" in prompt
    assert prompts.THREAD_CONVERSATION_REMINDER in prompt
    assert prompts.THREAD_CONVERSATION_REMINDER in chat_prompt
    assert "最新玩家邮件" not in task
    assert "通读整个 thread" not in task
    assert "Support workflow details live in the system prompt" in task


def test_auto_task_builder_includes_project_label_hints():
    task = build_auto_task(
        [
            {
                "message_id": "m1",
                "thread_id": "t1",
                "project_label": "BlackHole",
                "matched_labels": ["BlackHole/bug反馈"],
            }
        ],
        live_run=False,
    )

    assert "project_label=BlackHole" in task
    assert "BlackHole/bug反馈" in task
    assert "project hint" in task or "project_label" in task


def test_auto_task_builder_reprocesses_unread_existing_draft_without_new_draft():
    task = build_auto_task(
        [
            {
                "message_id": "m1",
                "thread_id": "t1",
                "project_label": "BlackHole",
                "matched_labels": ["BlackHole"],
                "reprocess_gmail_unread": True,
                "existing_status": "draft_created",
                "existing_draft_id": "draft-1",
                "existing_issue_type": "crash_or_freeze",
                "existing_recommended_labels": ["BlackHole", "BlackHole/崩溃卡死"],
                "existing_labels_applied": [],
            }
        ],
        live_run=True,
    )

    assert "Gmail is still UNREAD" in task
    assert "existing_draft_id=draft-1" in task
    assert "do not call create_gmail_draft" in task
    assert "preserve that draft_id" in task
    assert "apply_existing_gmail_labels" in task
    assert "mark_gmail_messages_read" in task
    assert "save_case_state" in task


@pytest.mark.asyncio
async def test_auto_worker_fetches_unread_project_messages_without_query(monkeypatch):
    class FakeGmailTools:
        def __init__(self, config):
            pass

        async def list_unread_project_emails(self, max_results_per_label=10):
            return {
                "messages": [
                    {
                        "message_id": "m1",
                        "thread_id": "t1",
                        "project_label": "BlackHole",
                        "matched_labels": ["BlackHole/bug反馈"],
                    }
                ]
            }

        async def get_message_internal_dates(self, message_ids):
            return {message_id: "1000" for message_id in message_ids}

    monkeypatch.setattr(auto_worker, "GmailTools", FakeGmailTools)

    result = await auto_worker.fetch_new_message_ids(
        SupportAgentConfig(),
        max_results=10,
    )

    assert result == [
        {
            "message_id": "m1",
            "thread_id": "t1",
            "project_label": "BlackHole",
            "matched_labels": ["BlackHole/bug反馈"],
            "internal_date": "1000",
        }
    ]


@pytest.mark.asyncio
async def test_auto_worker_discovery_only_does_not_call_model(monkeypatch, tmp_path):
    async def fake_fetch(config, *, max_results, query=None):
        return [{"message_id": "m1", "thread_id": "t1", "project_label": "BlackHole"}]

    class FailRunner:
        def __init__(self, *args, **kwargs):
            raise AssertionError("model runner should not be constructed")

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    monkeypatch.setattr(auto_worker, "SupportAgentRunner", FailRunner)

    result = await auto_worker.run_once(
        support_config=SupportAgentConfig(),
        run_config=AgentRunConfig(),
        store=ProcessedMessageStore(tmp_path / "manual.json"),
        max_candidates=10,
        max_new=1,
        retry_failed=False,
        max_retries=3,
        live_run=False,
        discovery_only=True,
        status_sink=lambda _: None,
    )

    assert result["status"] == "discovery_only"
    assert result["candidate_count"] == 1
    assert result["selected_count"] == 0
    assert result["candidates"][0]["project_label"] == "BlackHole"


@pytest.mark.asyncio
async def test_manual_trigger_readiness_check_exits_before_processing(
    monkeypatch,
    capsys,
):
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        manual_trigger,
        "parse_args",
        lambda: SimpleNamespace(
            config="config.toml",
            profile="cloud",
            backend=None,
            model=None,
            gguf=None,
            base_url=None,
            api_key=None,
            api_key_env=None,
            api_key_file=None,
            llamafile_mode=None,
            timeout_seconds=None,
            budget_tokens=None,
            max_iterations=None,
            block_db_in_dry_run=False,
            readiness_check=True,
            readiness_include_discovery=True,
            discovery_only=False,
            ignore_store=False,
            use_config_store=False,
            store_path=manual_trigger.DEFAULT_MANUAL_STORE,
            max_candidates=20,
            max_new=1,
            max_retries=3,
            retry_failed=False,
            live=False,
            query=None,
        ),
    )
    monkeypatch.setattr(
        manual_trigger,
        "load_config",
        lambda path: SupportAgentConfig(
            model_profiles={
                "cloud": ModelConfig(
                    backend="openai-compatible",
                    model="support-cloud",
                    base_url="https://provider.example/v1",
                    api_key_env="SUPPORT_MODEL_API_KEY",
                )
            }
        ),
    )
    monkeypatch.setenv("SUPPORT_MODEL_API_KEY", "model-key")

    async def fake_readiness(config, **kwargs):
        captured.update(kwargs)
        return {"status": "ready", "checks": []}

    monkeypatch.setattr(manual_trigger, "run_readiness_checks", fake_readiness)

    def fail_store(*args, **kwargs):
        raise AssertionError("readiness check should exit before store setup")

    monkeypatch.setattr(manual_trigger, "ProcessedMessageStore", fail_store)

    await manual_trigger.main_async()

    output = capsys.readouterr().out
    assert "Readiness: READY" in output
    assert captured["model_backend"] == "openai-compatible"
    assert captured["include_discovery"] is True


@pytest.mark.asyncio
async def test_auto_worker_readiness_check_exits_before_processing(monkeypatch, capsys):
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        auto_worker,
        "parse_args",
        lambda: SimpleNamespace(
            config="config.toml",
            backend="llamaserver",
            model="local-model",
            gguf="/tmp/model.gguf",
            base_url="http://localhost:8080/v1",
            llamafile_mode="prompt",
            timeout_seconds=60,
            budget_tokens=1024,
            max_iterations=4,
            block_db_in_dry_run=False,
            readiness_check=True,
            readiness_include_discovery=True,
            interval_seconds=0,
        ),
    )
    monkeypatch.setattr(auto_worker, "load_config", lambda path: SupportAgentConfig())

    async def fake_readiness(config, **kwargs):
        captured.update(kwargs)
        return {"status": "ready", "checks": []}

    monkeypatch.setattr(auto_worker, "run_readiness_checks", fake_readiness)

    def fail_store(*args, **kwargs):
        raise AssertionError("readiness check should exit before store setup")

    monkeypatch.setattr(auto_worker, "ProcessedMessageStore", fail_store)

    await auto_worker.main_async()

    output = capsys.readouterr().out
    assert "Readiness: READY" in output
    assert captured["gguf_path"] == "/tmp/model.gguf"
    assert captured["base_url"] == "http://localhost:8080/v1"
    assert captured["include_discovery"] is True


@pytest.mark.asyncio
async def test_auto_worker_records_discovery_failure(monkeypatch, tmp_path):
    async def fake_fetch(config, *, max_results, query=None):
        raise RuntimeError("gmail unavailable")

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)

    statuses: list[str] = []
    store = ProcessedMessageStore(tmp_path / "manual.json")
    result = await auto_worker.run_once(
        support_config=SupportAgentConfig(),
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=10,
        max_new=1,
        retry_failed=False,
        max_retries=3,
        live_run=False,
        status_sink=statuses.append,
    )

    assert result["status"] == "failed"
    assert result["stage"] == "discovery"
    assert "Gmail 新邮件检测失败" in statuses[-1]
    assert store._read()["runs"][0]["payload"]["stage"] == "discovery"


@pytest.mark.asyncio
async def test_auto_worker_ignore_store_processes_terminal_candidate(monkeypatch, tmp_path):
    async def fake_fetch(config, *, max_results, query=None):
        return [{"message_id": "m1", "thread_id": "t1", "project_label": "BlackHole"}]

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, task, status_sink=None, **kwargs):
            return AgentRunResult(
                answer="done",
                live_run=False,
                case_states=[
                    {
                        "case_id": "m1",
                        "status": "draft_created",
                        "data": {"draft_id": "d1"},
                    }
                ],
            )

    store = ProcessedMessageStore(tmp_path / "manual.json")
    store.record_seen([{"message_id": "m1", "thread_id": "t1"}])
    data = store._read()
    data["messages"]["m1"]["status"] = "draft_created"
    store._write(data)

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    monkeypatch.setattr(auto_worker, "SupportAgentRunner", FakeRunner)

    result = await auto_worker.run_once(
        support_config=SupportAgentConfig(),
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=10,
        max_new=1,
        retry_failed=False,
        max_retries=3,
        live_run=False,
        ignore_store=True,
        status_sink=lambda _: None,
    )

    assert result["selected_count"] == 1
    assert result["outcomes"][0]["status"] == "draft_created"


@pytest.mark.asyncio
async def test_auto_worker_reprocesses_unread_terminal_candidate(monkeypatch, tmp_path):
    async def fake_fetch(config, *, max_results, query=None):
        return [
            {
                "message_id": "m-new",
                "thread_id": "m-new",
                "project_label": "BlackHole",
                "internal_date": 200,
            },
            {
                "message_id": "m-old",
                "thread_id": "m-old",
                "project_label": "BlackHole",
                "internal_date": 100,
            },
        ]

    class FakeGmail:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_unread_message_ids(self, message_ids):
            return set(message_ids)

        async def get_message_internal_dates(self, message_ids):
            return {message_id: 0 for message_id in message_ids}

        async def aclose(self):
            return None

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, task, status_sink=None, **kwargs):
            return AgentRunResult(
                answer="done",
                live_run=False,
                case_states=[
                    {
                        "case_id": "m-new",
                        "status": "human_review",
                        "data": {"human_review_reason": "needs review"},
                    }
                ],
            )

    store = ProcessedMessageStore(tmp_path / "manual.json")
    store.record_seen(
        [
            {"message_id": "m-new", "thread_id": "m-new"},
            {"message_id": "m-old", "thread_id": "m-old"},
        ]
    )
    data = store._read()
    data["messages"]["m-new"]["status"] = "human_review"
    data["messages"]["m-old"]["status"] = "draft_created"
    store._write(data)

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    monkeypatch.setattr(auto_worker, "GmailTools", FakeGmail)
    monkeypatch.setattr(auto_worker, "SupportAgentRunner", FakeRunner)

    support_config = SupportAgentConfig()
    statuses: list[str] = []
    result = await auto_worker.run_once(
        support_config=support_config,
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=10,
        max_new=1,
        retry_failed=False,
        max_retries=3,
        live_run=False,
        status_sink=statuses.append,
    )

    assert result["selected_count"] == 1
    assert result["outcomes"][0]["message_id"] == "m-new"
    assert any("重跑 Gmail 仍为未读的本地已记录邮件" in line for line in statuses)


@pytest.mark.asyncio
async def test_auto_worker_skips_unread_failed_candidate_without_retry(
    monkeypatch,
    tmp_path,
):
    async def fake_fetch(config, *, max_results, query=None):
        return [
            {
                "message_id": "m1",
                "thread_id": "t1",
                "project_label": "BlackHole",
                "internal_date": 100,
            }
        ]

    class FakeGmail:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_unread_message_ids(self, message_ids):
            return set(message_ids)

        async def get_message_internal_dates(self, message_ids):
            return {message_id: 0 for message_id in message_ids}

        async def aclose(self):
            return None

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, task, status_sink=None, **kwargs):
            raise AssertionError("failed messages must not be processed without retry")

    store = ProcessedMessageStore(tmp_path / "manual.json")
    store.record_seen([{"message_id": "m1", "thread_id": "t1"}])
    data = store._read()
    data["messages"]["m1"]["status"] = "failed"
    data["messages"]["m1"]["retry_count"] = 3
    store._write(data)

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    monkeypatch.setattr(auto_worker, "GmailTools", FakeGmail)
    monkeypatch.setattr(auto_worker, "SupportAgentRunner", FakeRunner)

    statuses: list[str] = []
    result = await auto_worker.run_once(
        support_config=SupportAgentConfig(),
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=10,
        max_new=1,
        retry_failed=False,
        max_retries=3,
        live_run=False,
        reprocess_failed_unread=False,
        status_sink=statuses.append,
    )

    assert result["status"] == "already_processed"
    assert result["selected_count"] == 0
    assert result["skipped_details"] == [
        {
            "message_id": "m1",
            "store_status": "failed",
            "gmail_unread": True,
            "reason": "failed_retry_disabled",
        }
    ]
    assert not any("重跑 Gmail 仍为未读的本地已记录邮件" in line for line in statuses)


@pytest.mark.asyncio
async def test_auto_worker_live_run_clears_candidate_store_state_before_selection(
    monkeypatch,
    tmp_path,
):
    async def fake_fetch(config, *, max_results, query=None):
        return [
            {
                "message_id": "m1",
                "thread_id": "t1",
                "project_label": "BlackHole",
                "matched_labels": ["BlackHole"],
                "internal_date": 100,
            }
        ]

    class FakeGmail:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_unread_message_ids(self, message_ids):
            return set(message_ids)

        async def aclose(self):
            return None

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, task, status_sink=None, stop_after_case_ids=None, **kwargs):
            assert "确认正式处理" in task
            assert stop_after_case_ids == {"m1"}
            return AgentRunResult(
                answer="done",
                live_run=True,
                case_states=[
                    {
                        "case_id": "m1",
                        "status": "draft_created",
                        "data": {"draft_id": "d1"},
                    }
                ],
            )

    store = ProcessedMessageStore(tmp_path / "manual.json")
    store.record_seen([{"message_id": "m1", "thread_id": "t1"}])
    data = store._read()
    data["messages"]["m1"].update(
        {
            "status": "failed",
            "retry_count": 3,
            "error_message": "MaxIterationsError",
            "agent_answer_preview": "old answer",
            "labels_applied": ["无内容"],
            "data": {"old": True},
        }
    )
    store._write(data)

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    monkeypatch.setattr(auto_worker, "GmailTools", FakeGmail)
    monkeypatch.setattr(auto_worker, "SupportAgentRunner", FakeRunner)

    statuses: list[str] = []
    result = await auto_worker.run_once(
        support_config=SupportAgentConfig(),
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=10,
        max_new=1,
        retry_failed=False,
        max_retries=3,
        live_run=True,
        status_sink=statuses.append,
    )

    assert result["status"] == "draft_created"
    assert result["selected_count"] == 1
    assert result["outcomes"][0]["message_id"] == "m1"
    stored = store._read()["messages"]["m1"]
    assert stored["status"] == "draft_created"
    assert stored["retry_count"] == 0
    assert stored["error_message"] is None
    assert any("正式处理前已清理本轮候选状态 1 封" in line for line in statuses)


@pytest.mark.asyncio
async def test_auto_worker_already_processed_summary_lists_candidates(monkeypatch, tmp_path):
    async def fake_fetch(config, *, max_results, query=None):
        return [
            {
                "message_id": "m1",
                "thread_id": "m1",
                "project_label": "BlackHole",
                "internal_date": 100,
            }
        ]

    class FakeGmail:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_unread_message_ids(self, _message_ids):
            return set()

        async def get_message_summaries(self, message_ids):
            return {
                "m1": {
                    "subject": "Already done",
                    "from": "player@example.com",
                }
            }

        async def aclose(self):
            return None

    store = ProcessedMessageStore(tmp_path / "manual.json")
    store.record_seen([{"message_id": "m1", "thread_id": "m1"}])
    data = store._read()
    data["messages"]["m1"]["status"] = "draft_created"
    store._write(data)

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    monkeypatch.setattr(auto_worker, "GmailTools", FakeGmail)

    result = await auto_worker.run_once(
        support_config=SupportAgentConfig(),
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=10,
        max_new=1,
        retry_failed=False,
        max_retries=3,
        live_run=False,
        status_sink=lambda _: None,
    )

    assert result["status"] == "already_processed"
    assert result["selected_count"] == 0
    summary = result["human_summary"]["text"]
    assert "候选均已处理" in summary
    assert "Already done" in summary
    assert "没有新的待处理邮件" not in summary


def test_format_auto_run_status_text_distinguishes_no_mail_from_no_content():
    assert format_auto_run_status_text("skipped") == "无新邮件"
    assert (
        format_auto_run_status_text(
            "skipped",
            outcomes=[{"status": "skipped", "labels_applied": ["无内容"]}],
        )
        == "新邮件无内容"
    )
    assert format_auto_run_status_text("draft_created") == "已创建草稿"
    assert (
        format_auto_run_status_text(
            "skipped",
            candidate_count=3,
            selected_count=0,
        )
        == "候选均已处理"
    )
    assert format_auto_run_status_text("already_processed") == "候选均已处理"


def test_processed_message_store_filters_completed_outcomes(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [
        {"message_id": "m1", "thread_id": "t1"},
        {"message_id": "m2", "thread_id": "t2"},
    ]

    store.record_seen(candidates)
    store.mark_outcomes(
        [candidates[0]],
        run_id="r1",
        answer="done",
        case_states=[
            {
                "case_id": "m1",
                "status": "draft_created",
                "data": {"draft_id": "d1"},
            }
        ],
    )

    selected = store.select_unprocessed(
        candidates,
        limit=10,
        retry_failed=False,
        max_retries=3,
    )

    assert selected == [candidates[1]]


def test_processed_message_store_keeps_legacy_processed_terminal(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [{"message_id": "m1", "thread_id": "t1"}]

    store.record_seen(candidates)
    data = store._read()
    data["messages"]["m1"]["status"] = "processed"
    store._write(data)

    selected = store.select_unprocessed(
        candidates,
        limit=10,
        retry_failed=False,
        max_retries=3,
    )

    assert selected == []


def test_processed_message_store_uses_project_label_from_case_state_data(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [{"message_id": "m1", "thread_id": "t1"}]
    store.record_seen(candidates)
    store.mark_processing(candidates, run_id="r1")

    outcomes = store.mark_outcomes(
        candidates,
        run_id="r1",
        answer="done",
        case_states=[
            {
                "case_id": "m1",
                "status": "human_review",
                "data": {
                    "project_label": "Grill Master",
                    "applied_labels": ["Grill Master", "Grill Master/广告问题"],
                },
            }
        ],
    )

    assert outcomes[0]["project_label"] == "Grill Master"


def test_processed_message_store_records_model_outcomes(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [
        {"message_id": "m1", "thread_id": "t1"},
        {"message_id": "m2", "thread_id": "t2"},
    ]
    store.record_seen(candidates)
    store.mark_processing(candidates, run_id="r1")

    outcomes = store.mark_outcomes(
        candidates,
        run_id="r1",
        answer="本轮已完成。",
        case_states=[
            {
                "case_id": "m1",
                "status": "draft_created",
                "data": {
                    "draft_id": "d1",
                    "applied_labels": ["NumberCrush/咨询"],
                },
            },
            {
                "case_id": "m2",
                "status": "human_review",
                "data": {"human_review_reason": "missing receipt"},
            },
        ],
    )

    assert summarize_run_status(outcomes) == "human_review"
    assert outcomes[0]["status"] == "draft_created"
    assert outcomes[0]["draft_id"] == "d1"
    assert outcomes[0]["labels_applied"] == ["NumberCrush/咨询"]
    assert outcomes[1]["status"] == "human_review"
    assert outcomes[1]["human_review_reason"] == "missing receipt"

    data = store._read()
    assert data["messages"]["m1"]["status"] == "draft_created"
    assert data["messages"]["m2"]["status"] == "human_review"


def test_processed_message_store_marks_missing_model_outcome_failed(tmp_path):
    store = ProcessedMessageStore(tmp_path / "processed.json")
    candidates = [{"message_id": "m1", "thread_id": "t1"}]
    store.record_seen(candidates)
    store.mark_processing(candidates, run_id="r1")

    outcomes = store.mark_outcomes(
        candidates,
        run_id="r1",
        answer="没有保存状态。",
        case_states=[],
    )

    assert summarize_run_status(outcomes) == "failed"
    assert outcomes == [
        {
            "message_id": "m1",
            "thread_id": "t1",
            "project_label": None,
            "matched_labels": [],
            "status": "failed",
            "draft_id": None,
            "labels_applied": [],
            "human_review_reason": None,
            "error_message": "Agent did not call save_case_state for this message.",
        }
    ]
    assert store._read()["messages"]["m1"]["retry_count"] == 1


def test_agent_runner_extracts_create_gmail_draft_result():
    message = Message(
        MessageRole.TOOL,
        json.dumps(
            {
                "draft_id": "r-123",
                "thread_id": "m1",
                "subject": "Re: Eat Everything",
            }
        ),
        MessageMeta(MessageType.TOOL_RESULT),
        tool_name="create_gmail_draft",
    )

    assert extract_draft_result(message) == {
        "dry_run": False,
        "draft_id": "r-123",
        "thread_id": "m1",
        "subject": "Re: Eat Everything",
    }


def test_build_message_observer_auto_saves_after_create_gmail_draft():
    case_states: list[dict] = []
    observer = build_message_observer(
        status_sink=lambda _: None,
        case_states=case_states,
        stop_after_case_ids={"m1"},
        live_run=False,
    )

    # Draft auto-save now appends without raising (allows model to continue to
    # apply labels then explicit save). Explicit save triggers the complete.
    observer(
        Message(
            MessageRole.TOOL,
            json.dumps(
                {
                    "draft_id": "r-456",
                    "thread_id": "m1",
                    "subject": "Re: timer feedback",
                }
            ),
            MessageMeta(MessageType.TOOL_RESULT),
            tool_name="create_gmail_draft",
        )
    )

    assert len(case_states) == 1
    assert case_states[0]["case_id"] == "m1"
    assert case_states[0]["status"] == "draft_created"
    assert case_states[0]["data"]["draft_id"] == "r-456"
    # auto_saved flag and dry_run come from the helper
    assert case_states[0].get("data", {}).get("auto_saved") is True
    assert case_states[0].get("dry_run") is True


def test_agent_runner_extracts_dry_run_case_state_result():
    message = Message(
        MessageRole.TOOL,
        json.dumps(
            {
                "dry_run": True,
                "tool": "save_case_state",
                "args": {
                    "case_id": "m1",
                    "status": "human_review",
                    "data": {"human_review_reason": "missing receipt"},
                },
            }
        ),
        MessageMeta(MessageType.TOOL_RESULT),
        tool_name="save_case_state",
    )

    assert extract_case_state_result(message) == {
        "case_id": "m1",
        "status": "human_review",
        "data": {"human_review_reason": "missing receipt"},
        "dry_run": True,
    }


@pytest.mark.asyncio
async def test_agent_runner_returns_saved_state_when_final_respond_format_fails(
    monkeypatch,
):
    class FakeWorkflowRunner:
        def __init__(self, *args, on_message=None, **kwargs):
            self.on_message = on_message

        async def run(self, workflow, user_message):
            self.on_message(
                Message(
                    MessageRole.TOOL,
                    json.dumps(
                        {
                            "dry_run": True,
                            "tool": "save_case_state",
                            "args": {
                                "case_id": "m1",
                                "status": "draft_created",
                                "data": {"draft_id": "dry-run"},
                            },
                        }
                    ),
                    MessageMeta(MessageType.TOOL_RESULT),
                    tool_name="save_case_state",
                )
            )
            raise ToolCallError(
                "Retries exhausted after 3 consecutive failed attempts",
                raw_response="本轮已完成。",
            )

    monkeypatch.setattr(agent_runner, "WorkflowRunner", FakeWorkflowRunner)

    runner = agent_runner.SupportAgentRunner(
        AgentRunConfig(),
        support_config=SupportAgentConfig(),
    )
    result = await runner.run("处理 message_id=m1", status_sink=lambda _: None)

    assert result.answer == "本轮已完成。"
    assert result.case_states == [
        {
            "case_id": "m1",
            "status": "draft_created",
            "data": {"draft_id": "dry-run"},
            "dry_run": True,
        }
    ]


@pytest.mark.asyncio
async def test_agent_runner_stops_after_expected_case_state(monkeypatch):
    class FakeWorkflowRunner:
        def __init__(self, *args, on_message=None, **kwargs):
            self.on_message = on_message

        async def run(self, workflow, user_message):
            assert workflow.name == "multi_project_support"
            assert workflow.terminal_tool == "save_case_state"
            assert "read_email_thread" in workflow.required_steps
            assert "Do not call respond." in user_message
            self.on_message(
                Message(
                    MessageRole.TOOL,
                    json.dumps(
                        {
                            "dry_run": True,
                            "tool": "save_case_state",
                            "args": {
                                "case_id": "m1",
                                "status": "draft_created",
                                "data": {"draft_id": "dry-run"},
                            },
                        }
                    ),
                    MessageMeta(MessageType.TOOL_RESULT),
                    tool_name="save_case_state",
                )
            )
            raise AssertionError("runner should stop once expected case state is saved")

    monkeypatch.setattr(agent_runner, "WorkflowRunner", FakeWorkflowRunner)

    runner = agent_runner.SupportAgentRunner(
        AgentRunConfig(),
        support_config=SupportAgentConfig(),
    )
    result = await runner.run(
        "处理 message_id=m1",
        status_sink=lambda _: None,
        stop_after_case_ids={"m1"},
    )

    assert result.answer == "自动处理已保存 1 个 case 状态。"
    assert result.case_states[0]["case_id"] == "m1"


def test_terminal_chat_records_interactive_run_state(tmp_path, capsys):
    class FakeRunner:
        def run_sync(self, question, memory=None):
            return AgentRunResult(answer="这是自然语言回答。", live_run=False)

    store = ProcessedMessageStore(tmp_path / "processed.json")

    exit_code = terminal_chat.ask_once(
        "帮我查看最新邮件",
        runner=FakeRunner(),
        memory=ChatMemory(),
        store=store,
    )
    capsys.readouterr()

    runs = store._read()["runs"]
    assert exit_code == 0
    assert [run["status"] for run in runs] == ["processing", "completed"]
    assert runs[0]["payload"]["mode"] == "interactive"
    assert runs[0]["payload"]["input_preview"] == "帮我查看最新邮件"
    assert runs[1]["payload"]["answer_preview"] == "这是自然语言回答。"


@pytest.mark.asyncio
async def test_auto_worker_no_new_mail_skips_model(monkeypatch, tmp_path):
    async def fake_fetch(*args, **kwargs):
        return []

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    config = SupportAgentConfig(
        state=StateConfig(processed_store_path=str(tmp_path / "processed.json"))
    )
    store = ProcessedMessageStore(config.state.processed_store_path)

    result = await auto_worker.run_once(
        support_config=config,
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=5,
        max_new=5,
        retry_failed=False,
        max_retries=3,
        live_run=False,
        status_sink=lambda _text: None,
    )

    assert result["status"] == "skipped"
    assert result["selected_count"] == 0


@pytest.mark.asyncio
async def test_auto_worker_calls_agent_with_natural_language_task(
    monkeypatch,
    tmp_path,
):
    candidates = [
        {
            "message_id": "m1",
            "thread_id": "t1",
            "project_label": "BlackHole",
        }
    ]
    captured: dict[str, str] = {}

    async def fake_fetch(*args, **kwargs):
        return candidates

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, task, status_sink=None, **kwargs):
            captured["task"] = task
            captured["auto_surface"] = kwargs.get("auto_surface")
            return AgentRunResult(
                answer="本轮已完成。",
                live_run=False,
                case_states=[
                    {
                        "case_id": "m1",
                        "status": "draft_created",
                        "data": {"draft_id": "d1"},
                    }
                ],
            )

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    monkeypatch.setattr(auto_worker, "SupportAgentRunner", FakeRunner)
    config = SupportAgentConfig(
        state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        notify=NotifyConfig(mode="file", output_dir=str(tmp_path / "handoffs")),
    )
    store = ProcessedMessageStore(config.state.processed_store_path)
    statuses: list[str] = []

    result = await auto_worker.run_once(
        support_config=config,
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=5,
        max_new=5,
        retry_failed=False,
        max_retries=3,
        live_run=False,
        status_sink=statuses.append,
    )

    assert result["status"] == "draft_created"
    assert result["outcomes"][0]["status"] == "draft_created"
    assert result["outcomes"][0]["draft_id"] == "d1"
    assert "message_id=m1 thread_id=t1" in captured["task"]
    assert "Support workflow details live in the system prompt" in captured["task"]
    assert captured["auto_surface"] == "auto"
    assert statuses[-1] == "[完成] 自动处理结果：已创建草稿"


@pytest.mark.asyncio
async def test_auto_worker_runs_selected_messages_one_at_a_time(
    monkeypatch,
    tmp_path,
):
    candidates = [
        {
            "message_id": "m1",
            "thread_id": "t1",
            "project_label": "BlackHole",
        },
        {
            "message_id": "m2",
            "thread_id": "t2",
            "project_label": "BusFever",
        },
        {
            "message_id": "m3",
            "thread_id": "t3",
            "project_label": "BlackHole",
        },
    ]
    calls: list[dict[str, object]] = []

    async def fake_fetch(*args, **kwargs):
        return candidates

    class FakeTrace:
        def __init__(self, *, run_id):
            self.run_id = run_id
            self.log_path = tmp_path / "traces" / f"{run_id}.jsonl"

        def message(self, message):
            pass

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(
            self,
            task,
            status_sink=None,
            stop_after_case_ids=None,
            run_trace=None,
            **kwargs,
        ):
            call = {
                "task": task,
                "stop_after_case_ids": set(stop_after_case_ids or set()),
                "trace_path": str(run_trace.log_path),
                "auto_surface": kwargs.get("auto_surface"),
            }
            calls.append(call)
            message_id = next(iter(call["stop_after_case_ids"]))
            return AgentRunResult(
                answer=f"done {message_id}",
                live_run=False,
                case_states=[
                    {
                        "case_id": message_id,
                        "status": "draft_created",
                        "data": {"draft_id": f"d-{message_id}"},
                    }
                ],
            )

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    monkeypatch.setattr(auto_worker, "SupportAgentRunner", FakeRunner)
    monkeypatch.setattr(auto_worker, "RunTrace", FakeTrace)
    config = SupportAgentConfig(
        state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        notify=NotifyConfig(mode="file", output_dir=str(tmp_path / "handoffs")),
    )
    store = ProcessedMessageStore(config.state.processed_store_path)

    result = await auto_worker.run_once(
        support_config=config,
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=5,
        max_new=3,
        retry_failed=False,
        max_retries=3,
        live_run=False,
        status_sink=lambda _text: None,
    )

    assert [call["stop_after_case_ids"] for call in calls] == [
        {"m1"},
        {"m2"},
        {"m3"},
    ]
    assert "message_id=m1" in calls[0]["task"]
    assert "message_id=m2" not in calls[0]["task"]
    assert "message_id=m2" in calls[1]["task"] and "message_id=m1" not in calls[1]["task"]
    assert "message_id=m3" in calls[2]["task"] and "message_id=m2" not in calls[2]["task"]
    assert result["status"] == "draft_created"
    assert result["selected_count"] == 3
    assert [outcome["draft_id"] for outcome in result["outcomes"]] == ["d-m1", "d-m2", "d-m3"]
    assert all(outcome["trace_path"].endswith(f"{outcome['message_id']}.jsonl") for outcome in result["outcomes"])
    assert {call["auto_surface"] for call in calls} == {"auto"}


@pytest.mark.asyncio
async def test_auto_worker_continues_after_single_message_failure(
    monkeypatch,
    tmp_path,
):
    candidates = [
        {"message_id": "m1", "thread_id": "t1", "project_label": "BlackHole"},
        {"message_id": "m2", "thread_id": "t2", "project_label": "BlackHole"},
    ]

    async def fake_fetch(*args, **kwargs):
        return candidates

    class FakeTrace:
        def __init__(self, *, run_id):
            self.run_id = run_id
            self.log_path = tmp_path / f"{run_id}.jsonl"

        def message(self, message):
            pass

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(
            self,
            task,
            status_sink=None,
            stop_after_case_ids=None,
            run_trace=None,
            **kwargs,
        ):
            message_id = next(iter(stop_after_case_ids))
            if message_id == "m1":
                raise RuntimeError("model loop exhausted")
            return AgentRunResult(
                answer="second done",
                live_run=False,
                case_states=[
                    {
                        "case_id": "m2",
                        "status": "human_review",
                        "data": {"human_review_reason": "needs review"},
                    }
                ],
            )

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    monkeypatch.setattr(auto_worker, "SupportAgentRunner", FakeRunner)
    monkeypatch.setattr(auto_worker, "RunTrace", FakeTrace)
    config = SupportAgentConfig(
        state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        notify=NotifyConfig(mode="file", output_dir=str(tmp_path / "handoffs")),
    )
    store = ProcessedMessageStore(config.state.processed_store_path)

    result = await auto_worker.run_once(
        support_config=config,
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=5,
        max_new=2,
        retry_failed=False,
        max_retries=3,
        live_run=False,
        status_sink=lambda _text: None,
    )

    assert result["status"] == "failed"
    assert [outcome["status"] for outcome in result["outcomes"]] == [
        "failed",
        "human_review",
    ]
    assert result["outcomes"][0]["error_message"] == "RuntimeError: model loop exhausted"
    assert result["outcomes"][1]["human_review_reason"] == "needs review"
    assert (tmp_path / "handoffs" / "m1.txt").exists()
    assert not (tmp_path / "handoffs" / "m2.txt").exists()


@pytest.mark.asyncio
async def test_auto_worker_skipped_no_content_reports_correct_status_text(
    monkeypatch,
    tmp_path,
):
    candidates = [
        {
            "message_id": "m1",
            "thread_id": "t1",
            "project_label": "BlackHole",
            "matched_labels": ["BlackHole"],
        }
    ]

    async def fake_fetch(*args, **kwargs):
        return candidates

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, task, status_sink=None, **kwargs):
            return AgentRunResult(
                answer="自动处理已保存 1 个 case 状态。",
                live_run=True,
                case_states=[
                    {
                        "case_id": "m1",
                        "status": "skipped",
                        "data": {"applied_labels": ["无内容"]},
                    }
                ],
            )

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    monkeypatch.setattr(auto_worker, "SupportAgentRunner", FakeRunner)
    config = SupportAgentConfig(
        state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        notify=NotifyConfig(mode="file", output_dir=str(tmp_path / "handoffs")),
    )
    store = ProcessedMessageStore(config.state.processed_store_path)
    statuses: list[str] = []

    result = await auto_worker.run_once(
        support_config=config,
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=5,
        max_new=1,
        retry_failed=False,
        max_retries=3,
        live_run=True,
        status_sink=statuses.append,
    )

    assert result["status"] == "skipped"
    assert result["outcomes"][0]["labels_applied"] == ["无内容"]
    assert statuses[-1] == "[完成] 自动处理结果：新邮件无内容"


@pytest.mark.asyncio
async def test_auto_worker_marks_missing_agent_outcome_failed(
    monkeypatch,
    tmp_path,
):
    candidates = [
        {
            "message_id": "m1",
            "thread_id": "t1",
            "project_label": "BlackHole",
        }
    ]

    async def fake_fetch(*args, **kwargs):
        return candidates

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, task, status_sink=None, **kwargs):
            return AgentRunResult(answer="本轮已完成。", live_run=False)

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    monkeypatch.setattr(auto_worker, "SupportAgentRunner", FakeRunner)
    config = SupportAgentConfig(
        state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        notify=NotifyConfig(mode="file", output_dir=str(tmp_path / "handoffs")),
    )
    store = ProcessedMessageStore(config.state.processed_store_path)
    statuses: list[str] = []

    result = await auto_worker.run_once(
        support_config=config,
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=5,
        max_new=5,
        retry_failed=False,
        max_retries=3,
        live_run=False,
        status_sink=statuses.append,
    )

    assert result["status"] == "failed"
    assert result["outcomes"][0]["status"] == "failed"
    assert "save_case_state" in result["outcomes"][0]["error_message"]
    assert result["failure_notifications"][0]["notification"]["mode"] == "file"
    assert (tmp_path / "handoffs" / "m1.txt").exists()
    assert any("[人工] 自动处理失败" in status for status in statuses)
    assert statuses[-1] == "[错误] 自动处理结果：处理失败"


@pytest.mark.asyncio
async def test_auto_worker_runner_exception_escalates_to_human(
    monkeypatch,
    tmp_path,
):
    candidates = [
        {
            "message_id": "m1",
            "thread_id": "t1",
            "project_label": "BlackHole",
        }
    ]

    async def fake_fetch(*args, **kwargs):
        return candidates

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, task, status_sink=None, **kwargs):
            raise RuntimeError("model unavailable")

    monkeypatch.setattr(auto_worker, "fetch_new_message_ids", fake_fetch)
    monkeypatch.setattr(auto_worker, "SupportAgentRunner", FakeRunner)
    config = SupportAgentConfig(
        state=StateConfig(processed_store_path=str(tmp_path / "processed.json")),
        notify=NotifyConfig(mode="file", output_dir=str(tmp_path / "handoffs")),
    )
    store = ProcessedMessageStore(config.state.processed_store_path)
    statuses: list[str] = []

    result = await auto_worker.run_once(
        support_config=config,
        run_config=AgentRunConfig(),
        store=store,
        max_candidates=5,
        max_new=5,
        retry_failed=False,
        max_retries=3,
        live_run=False,
        status_sink=statuses.append,
    )

    assert result["status"] == "failed"
    assert result["outcomes"][0]["error_message"] == "RuntimeError: model unavailable"
    assert result["failure_notifications"][0]["notification"]["mode"] == "file"
    assert (tmp_path / "handoffs" / "m1.txt").exists()
    assert store._read()["messages"]["m1"]["status"] == "failed"


@pytest.mark.asyncio
async def test_auto_worker_recover_stale_processing_command(monkeypatch, tmp_path, capsys):
    store_path = tmp_path / "processed.json"
    store = ProcessedMessageStore(store_path)
    store.record_seen([{"message_id": "m1", "thread_id": "t1"}])
    data = store._read()
    data["messages"]["m1"].update(
        {
            "status": "processing",
            "agent_run_id": "old-run",
            "last_processed_at": "2026-07-01T00:00:00+00:00",
        }
    )
    store._write(data)

    monkeypatch.setattr(
        auto_worker,
        "parse_args",
        lambda: SimpleNamespace(
            config="config.toml",
            profile=None,
            backend=None,
            model=None,
            gguf_path=None,
            gguf=None,
            base_url=None,
            api_key=None,
            api_key_env=None,
            api_key_file=None,
            llamafile_mode=None,
            timeout_seconds=None,
            budget_tokens=None,
            max_iterations=None,
            block_db_in_dry_run=False,
            readiness_check=False,
            readiness_include_discovery=False,
            recover_stale_processing=True,
            stale_after_minutes=60,
            recover_stale_status="failed",
        ),
    )
    monkeypatch.setattr(
        auto_worker,
        "load_config",
        lambda path: SupportAgentConfig(
            state=StateConfig(processed_store_path=str(store_path)),
        ),
    )
    monkeypatch.setattr(
        auto_worker,
        "datetime",
        SimpleNamespace(
            now=lambda tz=None: datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc),
        ),
    )

    await auto_worker.main_async()

    output = capsys.readouterr().out
    assert "Recovered stale processing records: 1" in output
    data = store._read()
    assert data["messages"]["m1"]["status"] == "failed"
    assert data["runs"][-1]["payload"]["mode"] == "recover_stale_processing"


def test_message_observer_does_not_enrich_cross_case_save_state():
    case_states: list[dict[str, object]] = []
    observer = build_message_observer(
        status_sink=lambda _text: None,
        case_states=case_states,
        stop_after_case_ids={"expected"},
    )

    observer(
        Message(
            role=MessageRole.TOOL,
            content=json.dumps(
                {
                    "project": "BlackHole",
                    "case_type": "bug",
                    "recommended_labels": ["BlackHole/bug反馈"],
                    "detected_language": "en",
                }
            ),
            metadata=MessageMeta(type=MessageType.TOOL_RESULT),
            tool_name="extract_feedback_claim",
        )
    )
    observer(
        Message(
            role=MessageRole.TOOL,
            content=json.dumps(
                {
                    "case_id": "other",
                    "status": "human_review",
                    "data": {"issue_type": "no_content"},
                }
            ),
            metadata=MessageMeta(type=MessageType.TOOL_RESULT),
            tool_name="save_case_state",
        )
    )

    assert case_states == [
        {
            "case_id": "other",
            "status": "human_review",
            "data": {"issue_type": "no_content"},
            "dry_run": False,
        }
    ]
