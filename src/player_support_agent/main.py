"""CLI entrypoint for the model-driven multi-project support agent."""

from __future__ import annotations

import argparse
import json
from typing import Any

from .agent_runner import (
    DEFAULT_CONFIG,
    LIVE_CONFIRMATION,
    AgentRunConfig,
    ChatMemory,
    SupportAgentRunner,
    add_model_cli_args,
    build_run_config_from_args,
)
from .tools.clickhouse_tools import ClickHouseTools
from .tools.config import SupportAgentConfig, load_config
from .tools.rule_tools import RuleTools
from .workflows import build_multi_project_chat_workflow


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def build_preflight_summary(config: SupportAgentConfig) -> dict[str, Any]:
    """Return a safe config summary without secrets or Gmail calls."""

    workflow = build_multi_project_chat_workflow(config)
    configured_labels = set(config.gmail.allowed_label_names)
    policy_labels = {
        label
        for labels in config.policy.label_by_case_type.values()
        for label in labels
    }
    clickhouse = ClickHouseTools(
        config.clickhouse,
        remove_ads_investigation_path=config.knowledge.remove_ads_investigation_path,
        coin_frenzy_investigation_path=config.knowledge.coin_frenzy_investigation_path,
    )
    rules = RuleTools(
        config.knowledge,
        default_language=config.policy.default_language,
    )
    schema_by_case = {
        case_type: clickhouse.get_clickhouse_schema(case_type)
        for case_type in sorted(config.clickhouse.case_type_tables)
    }
    configured_project_tables = {
        project: sorted(
            {
                table
                for tables in case_tables.values()
                for table in tables
            }
        )
        for project, case_tables in config.clickhouse.project_case_type_tables.items()
    }
    projects_without_clickhouse_tables = [
        project
        for project in config.gmail.project_label_names
        if project not in config.clickhouse.project_case_type_tables
    ]
    return {
        "gmail": {
            "user_id": config.gmail.user_id,
            "account_email": config.gmail.account_email,
            "feedback_query": config.gmail.feedback_query,
            "allowed_label_count": len(config.gmail.allowed_label_names),
            "project_label_names": config.gmail.project_label_names,
            "allow_existing_project_labels": (
                config.gmail.allow_existing_project_labels
            ),
            "scan_child_project_labels": config.gmail.scan_child_project_labels,
            "policy_labels_missing_from_allowed": sorted(
                policy_labels - configured_labels
            ),
            "uses_refresh_token": config.gmail.has_refresh_credentials(),
            "has_access_token_fallback": bool(
                config.gmail.access_token
                or config.gmail.access_token_env
                or config.gmail.access_token_file
            ),
        },
        "clickhouse": {
            "url": config.clickhouse.url,
            "database": config.clickhouse.database,
            "allowed_tables": sorted(config.clickhouse.allowed_schema),
            "case_types": sorted(config.clickhouse.case_type_tables),
            "project_case_type_tables": {
                project: sorted(case_types)
                for project, case_types in config.clickhouse.project_case_type_tables.items()
            },
            "configured_project_tables": configured_project_tables,
            "projects_without_clickhouse_tables": projects_without_clickhouse_tables,
            "max_rows": config.clickhouse.max_rows,
            "max_time_window_hours": config.clickhouse.max_time_window_hours,
            "require_project_for_queries": config.clickhouse.require_project_for_queries,
            "schema_by_case_type": schema_by_case,
        },
        "policy": {
            "case_type_count": len(config.policy.label_by_case_type),
            "label_suffix_case_type_count": len(
                config.policy.label_suffix_by_case_type
            ),
            "high_risk_case_types": config.policy.high_risk_case_types,
            "auto_draft_confidence_threshold": (
                config.policy.auto_draft_confidence_threshold
            ),
            "human_review_confidence_threshold": (
                config.policy.human_review_confidence_threshold
            ),
        },
        "knowledge": rules.get_support_knowledge_summary(),
        "workflow": {
            "name": workflow.name,
            "tool_count": len(workflow.tools),
            "required_steps": workflow.required_steps,
            "terminal_tools": sorted(workflow.terminal_tools),
        },
        "state": {
            "state_path": config.state.state_path,
            "audit_log_path": config.state.audit_log_path,
            "processed_store_path": config.state.processed_store_path,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the model-driven multi-project support agent."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument(
        "--ask",
        help="Natural-language request. If omitted, a generic processing request is used.",
    )
    parser.add_argument(
        "--thread-id",
        help="Optional thread id to include in the natural-language task.",
    )
    parser.add_argument(
        "--message-id",
        help="Optional message id to include in the natural-language task.",
    )
    parser.add_argument(
        "--max-emails",
        type=int,
        default=1,
        help="Maximum mail count to mention to the model in the default task.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow Gmail draft/label/state side effects. Gmail send is still unavailable.",
    )
    add_model_cli_args(parser, default_max_iterations=18)
    parser.add_argument(
        "--block-db-in-dry-run",
        action="store_true",
        help="Also simulate ClickHouse query execution during dry-run.",
    )
    return parser.parse_args()


def build_run_config(args: argparse.Namespace) -> AgentRunConfig:
    return build_run_config_from_args(
        args,
        allow_db_in_dry_run=not args.block_db_in_dry_run,
    )


def build_task(args: argparse.Namespace) -> str:
    prefix = f"{LIVE_CONFIRMATION}\n\n" if args.live else ""
    if args.ask:
        return prefix + args.ask
    if args.thread_id:
        target = f"thread_id={args.thread_id}"
        if args.message_id:
            target += f" message_id={args.message_id}"
        return (
            prefix
            + "请处理这个指定的玩家反馈邮件。"
            + target
            + "。请通过工具读取邮件线程，根据 Gmail 标签判断项目，后续业务判断全部由你完成。"
        )
    return (
        prefix
        + f"请根据配置检查并处理最多 {args.max_emails} 封所有项目的未读玩家反馈邮件。"
        "请通过 Gmail 工具自行搜索候选邮件，根据 Gmail 父标签判断项目；后续邮件类型、日志查询、标签、草稿和转人工决策全部由你完成。"
    )


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    if args.preflight:
        print(_json(build_preflight_summary(config)))
        return

    runner = SupportAgentRunner(build_run_config(args), support_config=config)
    result = runner.run_sync(build_task(args), memory=ChatMemory())
    print("\n回答：")
    print(result.answer)


if __name__ == "__main__":
    main()
