# Gmail 自动处理稳定性优化计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 解决自动轮巡中 `MaxIterationsError`、批量 message 串线、`processing` 残留堵塞、空内容邮件误转人工的问题。

**Architecture:** 自动 worker 仍可一次选出多封候选邮件，但每封候选都独立进入一个 Forge run。状态补充只在单 case 上下文中发生，失败 trace 和 stale processing 恢复都走显式、可审计路径。

**Tech Stack:** Python, Forge `WorkflowRunner`, pytest, JSON-backed local state store.

---

## Summary

核心策略：`max_new` 仍表示本轮最多处理多少封，但 worker 内部改为“一封邮件一个 Forge run”，并为每封失败留下可追踪 trace。

## Key Changes

- 自动处理改为逐封模型运行：
  - 修改 `auto_worker.run_once`：先选出最多 `max_new` 个候选，但对每个候选单独 `mark_processing -> build_auto_task([item]) -> SupportAgentRunner.run(stop_after_case_ids={message_id}) -> mark_outcomes/mark_failed`。
  - 单封失败只影响该 message，后续 selected 邮件继续处理。
  - `--max-new` 默认改为 `1`，Web 自动轮巡默认也改为 `1`；仍允许显式传更大值，但内部始终逐封调用模型。

- 修复状态串线：
  - 调整 `agent_runner.build_message_observer` 的 `last_extract_claim` 补充逻辑：只有在单 case 自动 run 中，且保存的 `case_id` 等于当前 `stop_after_case_ids` 唯一值时，才用 extract 结果补 `issue_type/recommended_labels/detected_language`。
  - 避免批量或跨 case 时把上一封邮件的 `case_type`、标签、语言写入当前 case。

- 自动 run trace：
  - `auto_worker` 为每封模型 run 创建 `RunTrace(run_id=f"{auto_run_id}-{message_id}")`。
  - run summary、failure handoff 中记录 `trace_path`。
  - trace 只使用现有 preview 机制，不增加 OAuth token、secret、完整邮件正文输出。

- 显式恢复 stale `processing`：
  - 在 `ProcessedMessageStore` 增加维护方法：查找超过阈值的 `processing` 记录，并显式恢复为 `failed` 或 `pending`。
  - 在 `auto_worker` 增加 opt-in CLI：
    - `--recover-stale-processing`
    - `--stale-after-minutes 120`
    - `--recover-stale-status failed|pending`，默认 `failed`
  - 该命令只改本地 store，不调用模型，不写 Gmail，不自动运行在普通轮巡中。

- 空内容邮件标签路径：
  - 确认并修正 `extract_feedback_claim`：`case_type=no_content` 始终推荐配置里的全局 `无内容` 标签，不要求存在 `BusFever/无内容` 这类项目子标签。
  - 调整 prompt/task 文案：no_content 路径固定为 minimal assess -> `decide_support_action(rule_action="apply_label_only")` -> apply `["无内容"]` -> mark read -> save skipped。
  - 在 workflow required steps 中加入 `assess_claim_credibility`，放在 `resolve_player_identity` 和 `decide_support_action` 之间，消除 prompt 与工具 prerequisite 的矛盾。

## Public Interfaces

- CLI 默认变化：
  - `auto_worker --max-new` 默认从批量值收敛为 `1`。
  - Web 自动轮巡默认 `max_new=1`。
- 新增维护命令：
  - `.venv/bin/python -m src.player_support_agent.auto_worker --recover-stale-processing --stale-after-minutes 120 --recover-stale-status failed`
  - 命令输出恢复的 message_id、旧 run_id、旧更新时间、新状态，并记录一条本地 run 记录。
- 不新增 Gmail 发送、删除、归档、创建 label 行为；ClickHouse 查询路径不变。

## Test Plan

- `tests/unit/test_player_support_entrypoints.py`
  - fake runner 选中 3 封时，应被调用 3 次，每次 task 只包含 1 个 `message_id`。
  - 第一封抛 `MaxIterationsError`、第二封成功保存、第三封 human_review：最终 outcomes 应是混合状态，而不是整批失败。
  - run payload 和 failure handoff 包含对应单封 `trace_path`。
  - Web/CLI 自动轮巡默认 `max_new=1`，显式传 `max_new=5` 时仍逐封执行 5 次。

- `tests/unit/test_run_selection.py`
  - `processing` 默认仍跳过，避免重叠处理。
  - 新维护方法只恢复超过阈值的 `processing`，未超时记录不变。
  - 恢复到 `failed` 时递增或保留 retry 策略按现有失败语义处理；恢复到 `pending` 时不伪造 agent 错误。

- `tests/unit/test_player_support_decisions.py`
  - BusFever 等任意项目的 `no_content` 在配置含 `no_content = ["无内容"]` 时推荐 `["无内容"]`。
  - `available_label_names` 未包含项目子标签时，不应把 no_content 转成人工原因。

- Focused verification：
  - `.venv/bin/python -m compileall src/player_support_agent`
  - `.venv/bin/python -m pytest tests/unit/test_player_support_entrypoints.py tests/unit/test_player_support_config.py tests/unit/test_player_support_gmail.py tests/unit/test_player_support_clickhouse.py tests/unit/test_support_rules.py tests/unit/test_player_support_decisions.py tests/unit/test_run_selection.py tests/unit/test_candidate_discovery_filter.py`

## Assumptions

- 默认采用“逐封模型 run + 外层连续 drain”，不再让一个 Forge run 同时处理多封邮件。
- stale `processing` 不自动释放，必须显式执行恢复命令，避免误伤仍在运行的 worker。
- 实现时先检查当前 dirty worktree，保留已有未提交改动，不回滚用户或其他 agent 的变更。
