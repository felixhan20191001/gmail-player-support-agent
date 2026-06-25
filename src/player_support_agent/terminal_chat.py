"""Terminal chat entrypoint for the model-driven multi-project support agent."""

from __future__ import annotations

import argparse
import asyncio

from .agent_runner import (
    DEFAULT_CONFIG,
    LIVE_CONFIRMATION,
    AgentRunConfig,
    ChatMemory,
    SupportAgentRunner,
    add_model_cli_args,
    build_run_config_from_args,
)
from .processed_message_store import (
    ProcessedMessageStore,
    new_interactive_run_id,
    summarize_interactive_run_status,
)


EXIT_WORDS = {"exit", "quit", "q", ":q", "退出", "结束"}


def ask_once(
    question: str,
    *,
    runner: SupportAgentRunner,
    memory: ChatMemory,
    store: ProcessedMessageStore | None = None,
) -> int:
    run_id = new_interactive_run_id()
    live_run = LIVE_CONFIRMATION in question
    if store is not None:
        store.record_interactive_run(
            run_id=run_id,
            status="processing",
            user_input=question,
            live_run=live_run,
        )
    try:
        result = runner.run_sync(question, memory=memory)
    except asyncio.TimeoutError:
        if store is not None:
            store.record_interactive_run(
                run_id=run_id,
                status="failed",
                user_input=question,
                live_run=live_run,
                error_message="TimeoutError",
            )
        print("[错误] 本次请求超时了，可以缩小查询范围后再试。")
        return 1
    except KeyboardInterrupt:
        if store is not None:
            store.record_interactive_run(
                run_id=run_id,
                status="failed",
                user_input=question,
                live_run=live_run,
                error_message="KeyboardInterrupt",
            )
        print("\n[停止] 已中断当前请求。")
        return 130
    except Exception as exc:
        if store is not None:
            store.record_interactive_run(
                run_id=run_id,
                status="failed",
                user_input=question,
                live_run=live_run,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        print(f"[错误] agent 运行失败：{type(exc).__name__}: {exc}")
        return 1

    if store is not None:
        store.record_interactive_run(
            run_id=run_id,
            status=summarize_interactive_run_status(result.case_states),
            user_input=question,
            live_run=result.live_run,
            answer=result.answer,
            case_states=result.case_states,
        )
    print("\n回答：")
    print(result.answer)
    return 0


def repl(runner: SupportAgentRunner, store: ProcessedMessageStore) -> None:
    memory = ChatMemory()
    print("多项目邮件 agent 终端入口已启动。")
    print("每次提问都会先经过本地模型；模型会自行决定是否调用 Gmail、ClickHouse 或规则库。")
    print(f"默认 dry-run；正式写 Gmail 必须在问题里包含“{LIVE_CONFIRMATION}”。输入“退出”结束。")
    while True:
        try:
            question = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question:
            continue
        if question.casefold() in EXIT_WORDS:
            return
        ask_once(question, runner=runner, memory=memory, store=store)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ask the model-driven multi-project support agent from a terminal."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    add_model_cli_args(parser, default_max_iterations=18)
    parser.add_argument(
        "--block-db-in-dry-run",
        action="store_true",
        help="Also simulate ClickHouse query execution during dry-run.",
    )
    parser.add_argument(
        "--ask",
        help="Run one natural-language request through the model and exit.",
    )
    return parser.parse_args()


def build_run_config(args: argparse.Namespace) -> AgentRunConfig:
    return build_run_config_from_args(
        args,
        allow_db_in_dry_run=not args.block_db_in_dry_run,
    )


def main() -> None:
    args = parse_args()
    runner = SupportAgentRunner(build_run_config(args))
    store = ProcessedMessageStore(runner.support_config.state.processed_store_path)
    if args.ask:
        raise SystemExit(
            ask_once(args.ask, runner=runner, memory=ChatMemory(), store=store)
        )
    repl(runner, store)


if __name__ == "__main__":
    main()
