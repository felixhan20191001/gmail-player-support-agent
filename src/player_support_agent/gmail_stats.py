"""Model-driven Gmail statistics helper.

Kept for compatibility with older commands, but it no longer queries Gmail
directly. The request is handed to the local model and Forge tools.
"""

from __future__ import annotations

import argparse

from .agent_runner import (
    DEFAULT_CONFIG,
    ChatMemory,
    SupportAgentRunner,
    add_model_cli_args,
    build_run_config_from_args,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask the agent for Gmail stats.")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument(
        "--label",
        help="Optional Gmail label name. If omitted, ask about all project labels.",
    )
    parser.add_argument("--include-unread", action="store_true")
    parser.add_argument("--include-pending", action="store_true")
    add_model_cli_args(parser, default_max_iterations=12)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.label:
        request = f"请统计 Gmail 标签 {args.label} 下有多少封邮件。"
    else:
        request = "请统计所有项目 Gmail 父标签下的邮件数量，并按项目汇总。"
    if args.include_unread:
        request += " 同时说明未读数量。"
    if args.include_pending:
        request += " 同时说明当前配置的待处理邮件数量。"
    run_config = build_run_config_from_args(args, allow_db_in_dry_run=True)
    runner = SupportAgentRunner(run_config)
    result = runner.run_sync(request, memory=ChatMemory())
    print(result.answer)


if __name__ == "__main__":
    main()
