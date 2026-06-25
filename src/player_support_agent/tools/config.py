"""Configuration for the player support tools.

Secrets can be supplied directly for local experiments, but environment
variables or files are preferred for anything durable.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..paths import (
    default_knowledge_rules_path,
    default_templates_dir,
    default_var_dir,
)


def _resolve_secret(
    direct_value: str | None,
    env_name: str | None,
    file_path: str | None,
    label: str,
) -> str:
    if direct_value:
        return direct_value
    if env_name:
        value = os.getenv(env_name)
        if value:
            return value
    if file_path:
        path = Path(file_path).expanduser()
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    raise ValueError(f"Missing secret: {label}")


def _has_secret(
    direct_value: str | None,
    env_name: str | None,
    file_path: str | None,
) -> bool:
    if direct_value:
        return True
    if env_name and os.getenv(env_name):
        return True
    if file_path:
        path = Path(file_path).expanduser()
        return path.exists() and bool(path.read_text(encoding="utf-8").strip())
    return False


class GmailConfig(BaseModel):
    """Gmail API configuration.

    ``user_id`` can be ``"me"`` or a configured Gmail account address. The
    OAuth token must belong to that account and include the needed Gmail scopes.
    """

    model_config = ConfigDict(extra="forbid")

    api_base_url: str = "https://gmail.googleapis.com/gmail/v1"
    user_id: str = "me"
    account_email: str | None = None
    oauth_token_url: str = "https://oauth2.googleapis.com/token"
    access_token: str | None = None
    access_token_env: str | None = "GMAIL_ACCESS_TOKEN"
    access_token_file: str | None = None
    client_id: str | None = None
    client_id_env: str | None = "GOOGLE_CLIENT_ID"
    client_id_file: str | None = None
    client_secret: str | None = None
    client_secret_env: str | None = "GOOGLE_CLIENT_SECRET"
    client_secret_file: str | None = None
    refresh_token: str | None = None
    refresh_token_env: str | None = "GMAIL_REFRESH_TOKEN"
    refresh_token_file: str | None = None
    # Gmail "Primary" tab only; excludes Promotions, Social, and Updates categories.
    feedback_query: str = (
        "is:unread in:inbox category:primary -in:spam -in:trash"
    )
    request_timeout_seconds: float = 30.0
    max_request_retries: int = 3
    retry_backoff_seconds: float = 1.5
    # Deprecated: unread-always-process is enforced in ProcessedMessageStore.
    # Kept for backward-compatible config files only.
    reprocess_unread_terminal: bool = True
    allowed_label_names: list[str] = Field(default_factory=list)
    project_label_names: list[str] = Field(default_factory=list)
    skip_non_project_candidates: bool = True
    non_project_sender_patterns: list[str] = Field(
        default_factory=lambda: [
            "email.anthropic.com",
            "notice@email.anthropic.com",
        ]
    )
    allow_existing_project_labels: bool = True
    scan_child_project_labels: bool = True
    processed_label_name: str | None = None
    drafted_label_name: str | None = None
    needs_human_label_name: str | None = None

    def resolve_access_token(self) -> str:
        return _resolve_secret(
            self.access_token,
            self.access_token_env,
            self.access_token_file,
            "Gmail OAuth access token",
        )

    def has_refresh_credentials(self) -> bool:
        return (
            _has_secret(self.client_id, self.client_id_env, self.client_id_file)
            and _has_secret(
                self.client_secret,
                self.client_secret_env,
                self.client_secret_file,
            )
            and _has_secret(
                self.refresh_token,
                self.refresh_token_env,
                self.refresh_token_file,
            )
        )

    def resolve_client_id(self) -> str:
        return _resolve_secret(
            self.client_id,
            self.client_id_env,
            self.client_id_file,
            "Google OAuth client id",
        )

    def resolve_client_secret(self) -> str:
        return _resolve_secret(
            self.client_secret,
            self.client_secret_env,
            self.client_secret_file,
            "Google OAuth client secret",
        )

    def resolve_refresh_token(self) -> str:
        return _resolve_secret(
            self.refresh_token,
            self.refresh_token_env,
            self.refresh_token_file,
            "Gmail OAuth refresh token",
        )


class ModelConfig(BaseModel):
    """Model backend configuration for the support agent brain."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["llamaserver", "ollama", "openai-compatible", "openai"] = "llamaserver"
    model: str | None = None
    gguf_path: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = "OPENAI_API_KEY"
    api_key_file: str | None = None
    llamafile_mode: Literal["native", "prompt", "auto"] = "prompt"
    timeout_seconds: int | None = None
    budget_tokens: int | None = None
    max_iterations: int | None = None

    def resolve_api_key(self) -> str:
        return _resolve_secret(
            self.api_key,
            self.api_key_env,
            self.api_key_file,
            "Cloud model API key",
        )

    def has_api_key(self) -> bool:
        return _has_secret(self.api_key, self.api_key_env, self.api_key_file)


class ClickHouseTableConfig(BaseModel):
    """Allowed schema for one ClickHouse table."""

    model_config = ConfigDict(extra="forbid")

    columns: list[str]
    player_id_columns: list[str] = Field(default_factory=lambda: ["player_id"])
    time_column: str = "event_time"


class ClickHouseEvidenceRecipeConfig(BaseModel):
    """Configured evidence query recipe for one support question."""

    model_config = ConfigDict(extra="forbid")

    id: str
    projects: list[str] = Field(default_factory=list)
    case_types: list[str] = Field(default_factory=list)
    table: str
    select_columns: list[str]
    event_names: list[str] = Field(default_factory=list)
    filters: dict[str, str] = Field(default_factory=dict)
    product_id_contains: list[str] = Field(default_factory=list)
    time_column: str | None = None
    player_column: str | None = None
    limit: int = 100
    supported_when: Literal["any_row", "no_rows"] = "any_row"


class ClickHouseConfig(BaseModel):
    """ClickHouse HTTP API configuration."""

    model_config = ConfigDict(extra="forbid")

    url: str = "http://localhost:8123"
    database: str | None = None
    username: str | None = None
    username_env: str | None = None
    password: str | None = None
    password_env: str | None = "CLICKHOUSE_PASSWORD"
    password_file: str | None = None
    allowed_schema: dict[str, ClickHouseTableConfig] = Field(default_factory=dict)
    case_type_tables: dict[str, list[str]] = Field(default_factory=dict)
    project_case_type_tables: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    project_platform_tables: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    evidence_recipes: list[ClickHouseEvidenceRecipeConfig] = Field(default_factory=list)
    skip_log_query_case_types: list[str] = Field(
        default_factory=lambda: [
            "ad_issue",
            "ad_promo_mismatch",
            "feature_request",
            "no_content",
        ],
    )
    require_project_for_queries: bool = False
    max_rows: int = 200
    max_time_window_hours: int = 168
    connect_timeout_seconds: float = 5.0
    query_timeout_seconds: float = 30.0

    def resolve_username(self) -> str | None:
        if self.username:
            return self.username
        if self.username_env:
            return os.getenv(self.username_env)
        return None

    def resolve_password(self) -> str | None:
        if self.password or self.password_env or self.password_file:
            return _resolve_secret(
                self.password,
                self.password_env,
                self.password_file,
                "ClickHouse password",
            )
        return None


class NotifyConfig(BaseModel):
    """Human support notification configuration."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "file", "webhook", "feishu", "smtp"] = "file"
    output_dir: str = str(default_var_dir() / "handoffs")
    webhook_url: str | None = None
    webhook_token: str | None = None
    webhook_token_env: str | None = None
    feishu_webhook_url: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_username_env: str | None = None
    smtp_password: str | None = None
    smtp_password_env: str | None = None
    smtp_from: str | None = None
    human_support_email: str | None = None

    def resolve_webhook_token(self) -> str | None:
        if self.webhook_token:
            return self.webhook_token
        if self.webhook_token_env:
            return os.getenv(self.webhook_token_env)
        return None

    def resolve_smtp_username(self) -> str | None:
        if self.smtp_username:
            return self.smtp_username
        if self.smtp_username_env:
            return os.getenv(self.smtp_username_env)
        return None

    def resolve_smtp_password(self) -> str | None:
        if self.smtp_password:
            return self.smtp_password
        if self.smtp_password_env:
            return os.getenv(self.smtp_password_env)
        return None


class SupportPolicyConfig(BaseModel):
    """Business policy used by decision tools."""

    model_config = ConfigDict(extra="forbid")

    label_by_case_type: dict[str, list[str]] = Field(default_factory=dict)
    label_suffix_by_case_type: dict[str, list[str]] = Field(default_factory=dict)
    high_risk_case_types: list[str] = Field(
        default_factory=lambda: [
            "ban_appeal",
            "chargeback",
            "refund",
            "payment",
            "account_security",
            "compensation",
        ]
    )
    auto_draft_confidence_threshold: float = 0.85
    human_review_confidence_threshold: float = 0.60
    auto_draft_without_logs_case_types: list[str] = Field(
        default_factory=lambda: ["ad_issue", "feature_request"],
    )
    default_language: str = "zh-CN"


class KnowledgeConfig(BaseModel):
    """Support knowledge-base rule and template locations."""

    model_config = ConfigDict(extra="forbid")

    rules_path: str = str(default_knowledge_rules_path())
    templates_dir: str = str(default_templates_dir())
    legacy_templates_path: str = "knowledge/legacy_reply_templates.toml"
    remove_ads_investigation_path: str = "knowledge/remove_ads_investigation.toml"
    coin_frenzy_investigation_path: str = "knowledge/coin_frenzy_investigation.toml"
    project_rules_paths: dict[str, str] = Field(default_factory=dict)
    project_templates_dirs: dict[str, str] = Field(default_factory=dict)
    project_profiles_paths: dict[str, str] = Field(default_factory=dict)
    project_profiles_dir: str | None = None
    max_rules: int = 5


class StateConfig(BaseModel):
    """Local state and audit paths."""

    model_config = ConfigDict(extra="forbid")

    state_path: str = str(default_var_dir() / "cases.json")
    audit_log_path: str = str(default_var_dir() / "audit.jsonl")
    processed_store_path: str = str(default_var_dir() / "processed_messages.json")


class SupportAgentConfig(BaseModel):
    """Top-level config for all tools."""

    model_config = ConfigDict(extra="forbid")

    model: ModelConfig = Field(default_factory=ModelConfig)
    model_profiles: dict[str, ModelConfig] = Field(default_factory=dict)
    gmail: GmailConfig = Field(default_factory=GmailConfig)
    clickhouse: ClickHouseConfig = Field(default_factory=ClickHouseConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    policy: SupportPolicyConfig = Field(default_factory=SupportPolicyConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    state: StateConfig = Field(default_factory=StateConfig)


def load_config(path: str | Path) -> SupportAgentConfig:
    """Load a TOML config file."""

    config_path = Path(path).expanduser()
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    return SupportAgentConfig.model_validate(data)
