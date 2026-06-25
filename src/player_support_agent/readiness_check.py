"""Readiness checks before running the automatic support worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from .agent_runner import (
    DEFAULT_CONFIG,
    add_model_cli_args,
    build_run_config_from_args,
    resolve_run_config,
    resolve_support_model_config,
    run_config_has_api_key,
)
from .tools.config import ClickHouseConfig, SupportAgentConfig, load_config
from .tools.gmail_tools import GmailTools


CheckStatus = Literal["ok", "warning", "error"]
_SECRET_VALUE_RE = re.compile(
    r"(?i)(access_token|refresh_token|client_secret|password|token|secret)=([^\s&]+)"
)
_BEARER_RE = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+")


@dataclass(frozen=True)
class ReadinessCheck:
    component: str
    status: CheckStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def _check(
    component: str,
    status: CheckStatus,
    message: str,
    **details: Any,
) -> ReadinessCheck:
    safe_details = {key: value for key, value in details.items() if value is not None}
    return ReadinessCheck(component, status, message, safe_details)


def _safe_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = _SECRET_VALUE_RE.sub(r"\1=<redacted>", text)
    return _BEARER_RE.sub("Bearer <redacted>", text)


def summarize_readiness(checks: list[ReadinessCheck]) -> str:
    if any(check.status == "error" for check in checks):
        return "blocked"
    if any(check.status == "warning" for check in checks):
        return "ready_with_warnings"
    return "ready"


def _has_access_token_fallback(config: SupportAgentConfig) -> bool:
    try:
        config.gmail.resolve_access_token()
    except Exception:
        return False
    return True


def check_static_config(
    config: SupportAgentConfig,
    *,
    gguf_path: str,
    model_backend: str = "llamaserver",
    cloud_api_key_configured: bool = False,
) -> list[ReadinessCheck]:
    checks: list[ReadinessCheck] = []

    if config.gmail.has_refresh_credentials():
        checks.append(_check("gmail_auth", "ok", "Gmail refresh-token credentials are configured."))
    elif _has_access_token_fallback(config):
        checks.append(
            _check(
                "gmail_auth",
                "warning",
                "Gmail is using an access-token fallback; long runs should use refresh-token credentials.",
            )
        )
    else:
        checks.append(_check("gmail_auth", "error", "No usable Gmail OAuth credentials found."))

    if config.gmail.project_label_names:
        checks.append(
            _check(
                "gmail_projects",
                "ok",
                "Configured Gmail project parent labels are present in config.",
                project_count=len(config.gmail.project_label_names),
            )
        )
    else:
        checks.append(
            _check(
                "gmail_projects",
                "warning",
                "No explicit Gmail project_label_names configured; all user labels may be treated as projects.",
            )
        )

    if config.gmail.allow_existing_project_labels:
        checks.append(
            _check(
                "gmail_labels",
                "ok",
                "Existing project labels can be applied after Gmail existence validation.",
            )
        )
    elif config.gmail.allowed_label_names:
        checks.append(
            _check(
                "gmail_labels",
                "warning",
                "Only statically allowed labels can be applied; multi-project labels may be too narrow.",
            )
        )
    else:
        checks.append(_check("gmail_labels", "error", "No safe Gmail label application policy configured."))

    if config.clickhouse.require_project_for_queries:
        checks.append(
            _check("clickhouse_policy", "ok", "ClickHouse queries require an inferred project.")
        )
    else:
        checks.append(
            _check(
                "clickhouse_policy",
                "warning",
                "ClickHouse project-less queries are allowed; multi-project trials should require project routing.",
            )
        )

    missing_project_tables = [
        project
        for project in config.gmail.project_label_names
        if project not in config.clickhouse.project_case_type_tables
    ]
    if missing_project_tables:
        checks.append(
            _check(
                "clickhouse_projects",
                "warning",
                "Some projects have no ClickHouse table mapping and will safely skip log queries.",
                missing_projects=missing_project_tables,
            )
        )
    else:
        checks.append(
            _check(
                "clickhouse_projects",
                "ok",
                "All configured Gmail projects have ClickHouse table mappings.",
            )
        )

    if config.policy.label_suffix_by_case_type:
        checks.append(
            _check(
                "label_policy",
                "ok",
                "Project-local label suffix recommendations are configured.",
                case_type_count=len(config.policy.label_suffix_by_case_type),
            )
        )
    else:
        checks.append(
            _check(
                "label_policy",
                "warning",
                "No label_suffix_by_case_type configured; recommended labels may be single-project only.",
            )
        )

    knowledge_missing = _knowledge_coverage_missing(config)
    if any(knowledge_missing.values()):
        checks.append(
            _check(
                "knowledge_coverage",
                "warning",
                "Some projects are missing project-specific rules, templates, or profiles.",
                **knowledge_missing,
            )
        )
    else:
        checks.append(
            _check(
                "knowledge_coverage",
                "ok",
                "Configured projects have project-specific knowledge coverage.",
            )
        )

    if config.notify.mode == "feishu" and not (
        config.notify.feishu_webhook_url or config.notify.webhook_url
    ):
        checks.append(_check("notify", "error", "Feishu notification mode needs a webhook URL."))
    elif config.notify.mode == "webhook" and not config.notify.webhook_url:
        checks.append(_check("notify", "error", "Webhook notification mode needs webhook_url."))
    elif config.notify.mode == "smtp" and not (
        config.notify.smtp_host and config.notify.human_support_email
    ):
        checks.append(_check("notify", "error", "SMTP notification mode needs host and recipient."))
    else:
        checks.append(_check("notify", "ok", f"Notification mode is {config.notify.mode}."))

    if model_backend in {"openai", "openai-compatible"}:
        if cloud_api_key_configured:
            checks.append(
                _check(
                    "model_api_key",
                    "ok",
                    "Cloud model API key is configured.",
                )
            )
        else:
            checks.append(
                _check(
                    "model_api_key",
                    "error",
                    "Cloud model backend requires an API key.",
                )
            )
    else:
        gguf = Path(gguf_path).expanduser()
        if gguf.exists():
            checks.append(_check("model_file", "ok", "Configured GGUF model file exists.", path=str(gguf)))
        else:
            checks.append(
                _check(
                    "model_file",
                    "warning",
                    "Configured GGUF model file was not found. This is okay only if your model server is already running with another model path.",
                    path=str(gguf),
                )
            )

    checks.extend(check_state_paths(config))
    return checks


def _knowledge_coverage_missing(config: SupportAgentConfig) -> dict[str, list[str]]:
    projects = config.gmail.project_label_names
    missing_rules = [
        project
        for project in projects
        if not _path_exists(config.knowledge.project_rules_paths.get(project))
    ]
    missing_templates = [
        project
        for project in projects
        if not _path_exists(config.knowledge.project_templates_dirs.get(project))
    ]
    missing_profiles = [
        project
        for project in projects
        if not _profile_path_exists(config, project)
    ]
    return {
        "missing_project_rules": missing_rules,
        "missing_project_templates": missing_templates,
        "missing_project_profiles": missing_profiles,
    }


def _path_exists(value: str | None) -> bool:
    return bool(value and Path(value).expanduser().exists())


def _profile_path_exists(config: SupportAgentConfig, project: str) -> bool:
    direct = config.knowledge.project_profiles_paths.get(project)
    if _path_exists(direct):
        return True
    if not config.knowledge.project_profiles_dir:
        return False
    return (
        Path(config.knowledge.project_profiles_dir).expanduser()
        / f"{project.replace('/', '_')}.toml"
    ).exists()


def check_state_paths(config: SupportAgentConfig) -> list[ReadinessCheck]:
    checks: list[ReadinessCheck] = []
    paths = [
        ("state_path", config.state.state_path),
        ("audit_log_path", config.state.audit_log_path),
        ("processed_store_path", config.state.processed_store_path),
        ("handoff_output_dir", config.notify.output_dir),
    ]
    for name, raw_path in paths:
        path = Path(raw_path).expanduser()
        directory = path if name == "handoff_output_dir" else path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".readiness_check.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as exc:
            checks.append(
                _check(
                    "state_paths",
                    "error",
                    f"{name} directory is not writable.",
                    path=str(directory),
                    error=_safe_error(exc),
                )
            )
        else:
            checks.append(
                _check(
                    "state_paths",
                    "ok",
                    f"{name} directory is writable.",
                    path=str(directory),
                )
            )
    return checks


async def check_model_server(base_url: str) -> ReadinessCheck:
    url = base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        return _check(
            "model_server",
            "error",
            "Local model server is not reachable through the OpenAI-compatible /models endpoint.",
            base_url=base_url,
            error=_safe_error(exc),
        )
    model_count = len(payload.get("data", [])) if isinstance(payload, dict) else None
    return _check("model_server", "ok", "Local model server is reachable.", model_count=model_count)


async def check_gmail(
    config: SupportAgentConfig,
    *,
    include_discovery: bool,
) -> list[ReadinessCheck]:
    checks: list[ReadinessCheck] = []
    gmail = GmailTools(config.gmail)
    try:
        labels = await gmail.get_existing_gmail_labels()
    except Exception as exc:
        return [
            _check(
                "gmail",
                "error",
                "Gmail labels could not be read.",
                error=_safe_error(exc),
            )
        ]

    existing_user_labels = {
        label.get("name")
        for label in labels.get("labels", [])
        if label.get("type") == "user" and label.get("name")
    }
    labels_by_parent: dict[str, list[str]] = labels.get("project_labels_by_parent", {})
    project_parents = set(labels.get("project_parent_labels", []))
    missing = [
        project
        for project in config.gmail.project_label_names
        if project not in existing_user_labels
        and not any(label.startswith(f"{project}/") for label in existing_user_labels)
    ]
    if missing:
        checks.append(
            _check(
                "gmail_labels",
                "error",
                "Some configured Gmail project labels do not exist.",
                missing_projects=missing,
            )
        )
    else:
        checks.append(
            _check(
                "gmail_labels",
                "ok",
                "Configured Gmail project labels exist.",
                project_count=len(project_parents),
            )
        )
        empty_projects = [
            project
            for project in config.gmail.project_label_names
            if not labels_by_parent.get(project)
        ]
        if empty_projects:
            checks.append(
                _check(
                    "gmail_discovery",
                    "warning",
                    "Some configured projects have no scannable existing labels.",
                    missing_projects=empty_projects,
                )
            )

    if include_discovery:
        try:
            discovery = await gmail.list_unread_project_emails(max_results_per_label=1)
        except Exception as exc:
            checks.append(
                _check(
                    "gmail_discovery",
                    "error",
                    "Gmail unread project discovery failed.",
                    error=_safe_error(exc),
                )
            )
        else:
            checks.append(
                _check(
                    "gmail_discovery",
                    "ok",
                    "Gmail unread project discovery succeeded.",
                    candidate_count=discovery.get("result_size_estimate", 0),
                    scanned_label_count=discovery.get("scanned_label_count", 0),
                )
            )
    return checks


def _escape_sql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")


async def check_clickhouse(config: ClickHouseConfig) -> list[ReadinessCheck]:
    if not config.allowed_schema:
        return [_check("clickhouse", "warning", "No ClickHouse allowed_schema configured.")]

    username = config.resolve_username()
    auth = (username, config.resolve_password() or "") if username is not None else None
    database = config.database or "default"
    tables = sorted(config.allowed_schema)
    table_list = ", ".join(f"'{_escape_sql_string(table)}'" for table in tables)
    query = (
        "SELECT table, name FROM system.columns "
        f"WHERE database = '{_escape_sql_string(database)}' "
        f"AND table IN ({table_list}) FORMAT JSONEachRow"
    )
    timeout = httpx.Timeout(
        connect=config.connect_timeout_seconds,
        read=config.query_timeout_seconds,
        write=config.query_timeout_seconds,
        pool=config.connect_timeout_seconds,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(config.url, content=query.encode("utf-8"), auth=auth)
            resp.raise_for_status()
    except Exception as exc:
        return [
            _check(
                "clickhouse",
                "error",
                "ClickHouse metadata query failed.",
                url=config.url,
                database=database,
                error=_safe_error(exc),
            )
        ]

    actual: dict[str, set[str]] = {}
    for line in resp.text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        actual.setdefault(row.get("table", ""), set()).add(row.get("name", ""))

    missing_tables = [table for table in tables if table not in actual]
    missing_columns = {
        table: [column for column in cfg.columns if column not in actual.get(table, set())]
        for table, cfg in config.allowed_schema.items()
    }
    missing_columns = {
        table: columns for table, columns in missing_columns.items() if columns
    }
    if missing_tables or missing_columns:
        return [
            _check(
                "clickhouse",
                "error",
                "Configured ClickHouse tables or columns are missing.",
                missing_tables=missing_tables,
                missing_columns=missing_columns,
            )
        ]

    return [
        _check(
            "clickhouse",
            "ok",
            "Configured ClickHouse tables and columns exist.",
            table_count=len(tables),
            database=database,
        )
    ]


async def run_readiness_checks(
    config: SupportAgentConfig,
    *,
    gguf_path: str,
    base_url: str,
    model_backend: str = "llamaserver",
    cloud_api_key_configured: bool = False,
    check_model: bool = True,
    check_gmail_api: bool = True,
    check_clickhouse_api: bool = True,
    include_discovery: bool = False,
) -> dict[str, Any]:
    checks = check_static_config(
        config,
        gguf_path=gguf_path,
        model_backend=model_backend,
        cloud_api_key_configured=cloud_api_key_configured,
    )
    if check_model:
        if model_backend in {"openai", "openai-compatible"}:
            checks.append(
                _check(
                    "model_server",
                    "ok" if cloud_api_key_configured else "error",
                    (
                        "Cloud model backend is configured."
                        if cloud_api_key_configured
                        else "Cloud model backend is missing an API key."
                    ),
                    base_url=base_url,
                )
            )
        else:
            checks.append(await check_model_server(base_url))
    if check_gmail_api:
        checks.extend(await check_gmail(config, include_discovery=include_discovery))
    if check_clickhouse_api:
        checks.extend(await check_clickhouse(config.clickhouse))

    return {
        "status": summarize_readiness(checks),
        "checks": [asdict(check) for check in checks],
    }


def format_readiness_report(report: dict[str, Any]) -> str:
    status_text = {
        "ready": "READY",
        "ready_with_warnings": "READY_WITH_WARNINGS",
        "blocked": "BLOCKED",
    }.get(str(report.get("status")), str(report.get("status")))
    lines = [f"Readiness: {status_text}"]
    for check in report.get("checks", []):
        marker = {"ok": "OK", "warning": "WARN", "error": "ERROR"}.get(
            check.get("status"),
            str(check.get("status")),
        )
        lines.append(f"[{marker}] {check.get('component')}: {check.get('message')}")
        details = check.get("details") or {}
        if details:
            preview = {
                key: value
                for key, value in details.items()
                if key
                in {
                    "project_count",
                    "table_count",
                    "candidate_count",
                    "scanned_label_count",
                    "missing_projects",
                    "missing_tables",
                    "database",
                    "path",
                    "base_url",
                }
            }
            if preview:
                lines.append(f"  details: {preview}")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check readiness before running the automatic support worker."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    add_model_cli_args(parser, default_max_iterations=28)
    parser.add_argument("--skip-model", action="store_true")
    parser.add_argument("--skip-gmail", action="store_true")
    parser.add_argument("--skip-clickhouse", action="store_true")
    parser.add_argument(
        "--include-discovery",
        action="store_true",
        help="Also run Gmail unread project candidate discovery.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full JSON report.")
    return parser.parse_args()


async def main_async() -> int:
    args = parse_args()
    config = load_config(args.config)
    run_config = build_run_config_from_args(args, allow_db_in_dry_run=True)
    model_config = resolve_support_model_config(config, run_config.profile)
    run_config = resolve_run_config(run_config, model_config)
    report = await run_readiness_checks(
        config,
        gguf_path=run_config.gguf_path or "",
        base_url=run_config.base_url or "",
        model_backend=run_config.backend or "llamaserver",
        cloud_api_key_configured=run_config_has_api_key(run_config),
        check_model=not args.skip_model,
        check_gmail_api=not args.skip_gmail,
        check_clickhouse_api=not args.skip_clickhouse,
        include_discovery=args.include_discovery,
    )
    if args.json:
        import json

        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_readiness_report(report))
    return 1 if report["status"] == "blocked" else 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
