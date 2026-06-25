"""Manual test trigger for Gmail discovery plus model processing."""

from __future__ import annotations

import argparse
import asyncio

from .agent_runner import (
    DEFAULT_CONFIG,
    add_model_cli_args,
    resolve_run_config,
    resolve_support_model_config,
    run_config_has_api_key,
)
from .auto_worker import build_run_config, run_once
from .paths import default_var_dir
from .processed_message_store import ProcessedMessageStore
from .readiness_check import format_readiness_report, run_readiness_checks
from .tools.config import load_config


DEFAULT_MANUAL_STORE = str(default_var_dir() / "manual_trigger_processed_messages.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manually trigger the automatic Gmail discovery flow and optionally "
            "hand discovered messages to the local model."
        )
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    add_model_cli_args(parser, default_max_iterations=28)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--max-new", type=int, default=1)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow Gmail draft/label/state side effects. Gmail send is still unavailable.",
    )
    parser.add_argument(
        "--block-db-in-dry-run",
        action="store_true",
        help="Also simulate ClickHouse query execution during dry-run.",
    )
    parser.add_argument(
        "--query",
        help="Optional Gmail candidate query override for discovery testing.",
    )
    parser.add_argument(
        "--ignore-store",
        action="store_true",
        help="Process discovered candidates even if this manual store already marks them done.",
    )
    parser.add_argument(
        "--discovery-only",
        action="store_true",
        help="Only run Gmail candidate discovery and do not call the model.",
    )
    parser.add_argument(
        "--store-path",
        default=DEFAULT_MANUAL_STORE,
        help="Manual trigger state path. Defaults to a separate test store.",
    )
    parser.add_argument(
        "--use-config-store",
        action="store_true",
        help="Use the configured automatic processed-message store instead of the test store.",
    )
    parser.add_argument(
        "--readiness-check",
        action="store_true",
        help="Check model, Gmail, ClickHouse, labels, and local state before processing.",
    )
    parser.add_argument(
        "--readiness-include-discovery",
        action="store_true",
        help="Also run Gmail unread-project discovery during --readiness-check.",
    )
    return parser.parse_args()


async def main_async() -> None:
    args = parse_args()
    support_config = load_config(args.config)
    run_config = build_run_config(args)
    if args.readiness_check:
        model_config = resolve_support_model_config(
            support_config,
            run_config.profile,
        )
        resolved_run_config = resolve_run_config(run_config, model_config)
        report = await run_readiness_checks(
            support_config,
            gguf_path=resolved_run_config.gguf_path or "",
            base_url=resolved_run_config.base_url or "",
            model_backend=resolved_run_config.backend or "llamaserver",
            cloud_api_key_configured=run_config_has_api_key(resolved_run_config),
            include_discovery=args.readiness_include_discovery,
        )
        print(format_readiness_report(report))
        if report["status"] == "blocked":
            raise SystemExit(1)
        return
    store_path = (
        support_config.state.processed_store_path
        if args.use_config_store
        else args.store_path
    )
    store = ProcessedMessageStore(store_path)
    runner_preview = resolve_run_config(
        run_config,
        resolve_support_model_config(support_config, run_config.profile),
    )
    profile_label = f" profile={run_config.profile}" if run_config.profile else ""
    print(
        f"[手动] 模型：backend={runner_preview.backend} "
        f"model={runner_preview.model or '(default)'}{profile_label}"
    )
    print(f"[手动] 使用状态文件：{store_path}")
    if args.discovery_only:
        print("[手动] 只检测候选邮件，不调用模型")
    elif args.ignore_store:
        print("[手动] 忽略测试状态去重，发现候选后会交给模型")

    result = await run_once(
        support_config=support_config,
        run_config=run_config,
        store=store,
        max_candidates=args.max_candidates,
        max_new=args.max_new,
        retry_failed=args.retry_failed,
        max_retries=args.max_retries,
        live_run=args.live,
        query=args.query,
        ignore_store=args.ignore_store,
        discovery_only=args.discovery_only,
    )
    if result.get("candidates"):
        print("\n候选邮件：")
        for item in result["candidates"]:
            labels = ", ".join(item.get("matched_labels", []))
            print(
                f"- message_id={item.get('message_id')} "
                f"thread_id={item.get('thread_id')} "
                f"project={item.get('project_label') or ''} "
                f"labels={labels}"
            )
    if result.get("answer"):
        print("\n回答：")
        print(result["answer"])


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
