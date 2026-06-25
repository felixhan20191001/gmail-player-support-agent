# Gmail 玩家反馈 Agent 快速接手说明

最后更新：2026-06-17

本文档给后续 Codex 或其他 coding agent 快速理解
`examples/player_support_agent` 项目。完整背景见：

- `examples/player_support_agent/README.md`
- `examples/player_support_agent/docs/PROJECT_PLAN.md`
- `examples/player_support_agent/docs/OPTIMIZATION_BACKLOG.md`
- 仓库根目录 `AGENTS.md`

## 项目一句话

这是一个基于 Forge `WorkflowRunner` 的多项目 Gmail 玩家客服 Agent。它读取 Gmail 玩家反馈邮件，结合 Gmail 标签、规则库、回复模板和 ClickHouse 日志，让模型判断问题类型、证据可信度、标签、草稿或转人工动作。目前只允许创建 Gmail 草稿，不允许自动发送邮件。

## 当前目标

- 支持用户主动向 agent 提问，例如查看未读邮件、总结邮件、处理指定 thread/message。
- 支持自动/手动触发 Gmail 新邮件发现，然后交给模型处理。
- 支持多项目：Gmail 父标签视为项目名，例如 `NumberCrush`、`BlackHole`。
- 支持本地模型和 OpenAI-compatible 云模型配置。
- 让模型根据玩家实际反馈语言生成对应语言的草稿。
- 对失败、低置信、证据不足或工具异常的 case 转人工。

## 关键架构原则

入口层不能用关键词、正则或 if/else 直接决定业务行为。用户主动请求必须进入：

```text
SupportAgentRunner -> Forge WorkflowRunner -> model -> registered tools
```

自动 worker 只能直接做这些事：

- Gmail 候选邮件 ID 发现。
- 去重和重试状态判断。
- 构造自然语言任务。
- 调用 `SupportAgentRunner`。

邮件分类、SQL、标签、草稿、转人工等业务判断都必须由模型通过工具完成。

## 主要入口

| 文件 | 用途 |
| --- | --- |
| `main.py` | 一次性自然语言请求和 preflight。 |
| `terminal_chat.py` | 终端主动对话。 |
| `chat_server.py` | 本地 Web 控制 UI。 |
| `auto_worker.py` | 自动发现 Gmail 候选并处理。 |
| `manual_trigger.py` | 手动模拟“Gmail discovery -> 模型处理”。 |
| `readiness_check.py` | 检查模型、Gmail、ClickHouse、状态目录和配置。 |

## 主要模块

| 文件 | 职责 |
| --- | --- |
| `agent_runner.py` | 模型客户端配置、`SupportAgentRunner`、Forge runner 封装、状态输出。 |
| `workflows.py` | 构建 interactive/automatic workflow。 |
| `prompts.py` | 模型运行时策略和工具使用要求。 |
| `auto_task_builder.py` | 自动处理时给模型的自然语言任务。 |
| `processed_message_store.py` | 自动处理去重、状态、失败重试记录。 |
| `tools/forge_tools.py` | 注册 Gmail/ClickHouse/rules/decision/state/notify 工具。 |
| `tools/gmail_tools.py` | Gmail 搜索、线程读取、已有标签应用、草稿创建。 |
| `tools/clickhouse_tools.py` | 白名单 schema、SQL 校验、只读查询、日志摘要。 |
| `tools/rule_tools.py` | 规则和回复模板读取。 |
| `tools/decision_tools.py` | 诉求提取、身份解析、可信度和动作判断结构化工具。 |
| `tools/state_tools.py` | 模型保存 case 状态和审计记录。 |
| `tools/notify_tools.py` | 转人工通知。 |
| `tools/config.py` | TOML 配置模型，包括 Gmail、ClickHouse、模型、策略、知识库和状态路径。 |

## Gmail 安全边界

Gmail 工具允许：

- 搜索邮件。
- 读取邮件线程。
- 读取已有 Gmail 标签。
- 应用已存在且通过校验的标签。
- 创建 Gmail 草稿。

Gmail 工具禁止：

- 发送邮件。
- 删除、归档、移动到垃圾箱。
- 创建 Gmail 标签。
- 打印 OAuth token、refresh token、client secret 或完整私密邮件正文。

## ClickHouse 安全边界

ClickHouse 工具必须：

- 只允许 `SELECT`。
- 查询白名单表和字段。
- 有玩家标识、时间范围和 `LIMIT`。
- 多项目时优先要求 `require_project_for_queries = true`。
- 项目没有表映射时 fail closed，不回退到其他项目表。

## 模型配置

本地模型默认使用 llama-server：

```toml
[model]
backend = "llamaserver"
gguf_path = "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf"
base_url = "http://localhost:8080/v1"
llamafile_mode = "prompt"
```

云模型使用 OpenAI-compatible API：

```toml
[model]
backend = "openai-compatible"
model = "your-cloud-model"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
```

API key 优先放环境变量或文件，不要写进日志或提交到仓库。

## 常用命令

Readiness check：

```bash
.venv/bin/python -m examples.player_support_agent.readiness_check \
  --config examples/player_support_agent/config.local.toml
```

手动 discovery-only：

```bash
.venv/bin/python -m examples.player_support_agent.manual_trigger \
  --config examples/player_support_agent/config.local.toml \
  --max-candidates 20 \
  --max-new 1 \
  --discovery-only
```

手动 live 草稿测试：

```bash
.venv/bin/python -m examples.player_support_agent.manual_trigger \
  --config examples/player_support_agent/config.local.toml \
  --max-candidates 20 \
  --max-new 1 \
  --live \
  --retry-failed
```

终端主动对话：

```bash
.venv/bin/python -m examples.player_support_agent.terminal_chat \
  --config examples/player_support_agent/config.local.toml
```

## 语言处理要求

模型读取邮件后，应根据玩家实际反馈内容识别回复语言：

- 优先看 `My question is:` 后面的自由文本。
- 忽略 platform、version、userid、邮件头等模板字段。
- 如果邮件中出现英语以外的玩家自然语言，优先使用该语言回复。
- `extract_feedback_claim` 应保留 `detected_language` 和 `language_source_text`。
- 创建草稿时必须使用玩家语言，不额外添加未知签名。

## 自动处理状态要求

自动处理 selected message 后，模型必须调用 `save_case_state`。允许状态：

- `draft_created`
- `human_review`
- `failed`
- `skipped`

如果模型没有为 selected message 保存状态，worker 必须标记为 `failed`，并按配置转人工或写 handoff 文件。

## 当前项目状态（2026-06-17）

- **阶段**：live 小流量草稿验证中；207 个聚焦单测通过。
- **架构**：入口与 worker 均遵守 `AGENTS.md`；业务决策在模型 + Forge 工具层。
- **近期修复**：`MaxIterationsError` / 计时器功能建议邮件（`19ed15852981a702`）——收紧证据目录与 Coin Frenzy 分支、`feature_request` 跳过查库、规则 `guidance`、`create_gmail_draft` 后自动 `save_case_state`。
- **待验证**：对上述 message_id 再跑 `--live --max-new 1`，预期 `feature_request_ack` 英文草稿 + `BlackHole/功能建议` + `draft_created`。
- **详细 backlog**：见 `docs/OPTIMIZATION_BACKLOG.md`。

## 当前可考虑的下一步

1. 对 `19ed15852981a702` 做 live 回归，确认自动处理不再误转人工。
2. 新增 `get_cloud_support_plan` 业务工具，让本地模型按需调用云模型作为顾问，而不是让云模型直接写 Gmail。
2. 为常见问题加入项目规则和模板，例如 NumberCrush Daily Puzzle blank/gray square。
3. 增加云模型调用缓存，按 `project + case_type + language + normalized_claim` 复用建议。
4. 补充更多语言识别和草稿语言测试。
5. 为 Gmail 读写超时增加更清晰的诊断和重试策略。

## 修改后验证

改动 player support agent 后，至少运行：

```bash
.venv/bin/python -m compileall src/player_support_agent
.venv/bin/python -m pytest \
  tests/unit/test_player_support_entrypoints.py \
  tests/unit/test_player_support_config.py \
  tests/unit/test_player_support_gmail.py \
  tests/unit/test_player_support_clickhouse.py \
  tests/unit/test_support_rules.py \
  tests/unit/test_player_support_decisions.py \
  tests/unit/test_run_selection.py \
  tests/unit/test_candidate_discovery_filter.py
```

如果修改了 Forge 共享框架或模型客户端，还要跑相关 Forge 单测。
