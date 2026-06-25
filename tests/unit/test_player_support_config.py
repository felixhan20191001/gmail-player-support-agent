from player_support_agent.readiness_check import (
    check_static_config,
    format_readiness_report,
    summarize_readiness,
)
from player_support_agent.tools.config import (
    ClickHouseConfig,
    GmailConfig,
    ModelConfig,
    NotifyConfig,
    SupportAgentConfig,
)


def test_gmail_refresh_credentials_require_actual_secret_values(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GMAIL_REFRESH_TOKEN", raising=False)

    config = GmailConfig(
        client_id_env="GOOGLE_CLIENT_ID",
        client_secret_env="GOOGLE_CLIENT_SECRET",
        refresh_token_env="GMAIL_REFRESH_TOKEN",
    )

    assert config.has_refresh_credentials() is False

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh-token")

    assert config.has_refresh_credentials() is True


def test_notify_config_accepts_feishu_mode():
    config = NotifyConfig(
        mode="feishu",
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/example",
    )

    assert config.mode == "feishu"
    assert config.feishu_webhook_url.endswith("/example")


def test_model_config_resolves_cloud_api_key_from_env(monkeypatch):
    monkeypatch.setenv("SUPPORT_MODEL_API_KEY", "model-key")

    config = ModelConfig(
        backend="openai-compatible",
        model="support-model",
        api_key_env="SUPPORT_MODEL_API_KEY",
    )

    assert config.has_api_key() is True
    assert config.resolve_api_key() == "model-key"


def test_support_agent_config_accepts_named_model_profiles():
    config = SupportAgentConfig.model_validate(
        {
            "model": {
                "backend": "llamaserver",
                "gguf_path": "/models/local.gguf",
                "base_url": "http://localhost:8080/v1",
            },
            "model_profiles": {
                "cloud": {
                    "backend": "openai-compatible",
                    "model": "support-cloud",
                    "base_url": "https://provider.example/v1",
                    "api_key_env": "SUPPORT_MODEL_API_KEY",
                }
            },
        }
    )

    assert config.model.backend == "llamaserver"
    assert config.model_profiles["cloud"].backend == "openai-compatible"
    assert config.model_profiles["cloud"].model == "support-cloud"


def test_readiness_static_config_reports_multi_project_warnings(tmp_path):
    config = SupportAgentConfig(
        gmail=GmailConfig(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
            project_label_names=["BlackHole"],
            allow_existing_project_labels=True,
        ),
        clickhouse=ClickHouseConfig(require_project_for_queries=True),
        notify=NotifyConfig(mode="file", output_dir=str(tmp_path / "handoffs")),
    )

    checks = check_static_config(config, gguf_path=str(tmp_path / "missing.gguf"))

    assert summarize_readiness(checks) == "ready_with_warnings"
    assert any(
        check.component == "gmail_auth" and check.status == "ok" for check in checks
    )
    assert any(
        check.component == "clickhouse_policy" and check.status == "ok"
        for check in checks
    )
    assert any(
        check.component == "clickhouse_projects" and check.status == "warning"
        for check in checks
    )


def test_readiness_static_config_checks_cloud_api_key(tmp_path):
    config = SupportAgentConfig(
        gmail=GmailConfig(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
            project_label_names=["BlackHole"],
            allow_existing_project_labels=True,
        ),
        clickhouse=ClickHouseConfig(require_project_for_queries=True),
        notify=NotifyConfig(mode="file", output_dir=str(tmp_path / "handoffs")),
    )

    missing_key = check_static_config(
        config,
        gguf_path=str(tmp_path / "missing.gguf"),
        model_backend="openai-compatible",
        cloud_api_key_configured=False,
    )
    configured_key = check_static_config(
        config,
        gguf_path=str(tmp_path / "missing.gguf"),
        model_backend="openai-compatible",
        cloud_api_key_configured=True,
    )

    assert any(
        check.component == "model_api_key" and check.status == "error"
        for check in missing_key
    )
    assert any(
        check.component == "model_api_key" and check.status == "ok"
        for check in configured_key
    )
    assert not any(check.component == "model_file" for check in missing_key)


def test_readiness_report_keeps_sensitive_error_details_out_of_terminal():
    report = {
        "status": "blocked",
        "checks": [
            {
                "component": "gmail",
                "status": "error",
                "message": "Gmail labels could not be read.",
                "details": {
                    "error": "refresh_token=secret-value",
                    "base_url": "http://localhost:8080/v1",
                },
            }
        ],
    }

    text = format_readiness_report(report)

    assert "Readiness: BLOCKED" in text
    assert "base_url" in text
    assert "secret-value" not in text
    assert "refresh_token" not in text
