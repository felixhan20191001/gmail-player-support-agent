# Gmail 玩家反馈 AI Agent 整体计划

最后更新：2026-06-17

本文档用于记录当前“本地模型 + Forge WorkflowRunner + Gmail + ClickHouse + 人工干预”的玩家反馈处理 Agent 的整体设计、运行模式、阶段计划和验收标准。后续改代码时，优先以本文档、`AGENTS.md`、`SKILL.md` 和运行时 prompt/工具描述为共同参考。

## 1. 项目目标

构建一个可自动处理、也可人工主动对话干预的 Gmail 玩家反馈处理 Agent。

核心目标：

- 使用本地模型作为业务决策大脑。
- 使用 Forge `WorkflowRunner` 作为 Agent 主循环。
- 通过 Gmail 工具读取邮件、应用已有标签、创建回复草稿。
- 通过 ClickHouse 工具查询玩家行为日志，辅助判断玩家反馈可信度。
- 通过规则库、模板库和项目配置实现多项目差异化回复。
- 对高风险、证据不足、工具失败或模型不确定的 case 转人工处理。
- 当前阶段只创建草稿，不自动发送邮件。

## 2. 核心原则

1. 主动对话必须走模型。
   CLI/Web 入口只接收用户输入，调用 `SupportAgentRunner`，不通过关键词直接决定 Gmail、ClickHouse、SQL、标签或草稿动作。

2. 自动处理只允许在调度层发现候选邮件。
   `auto_worker` 可以直接调用 Gmail 发现未读候选 ID、去重、构造自然语言任务，然后必须交给模型处理。

3. Gmail 只使用已有标签。
   工具层禁止创建标签、发送邮件、删除邮件、归档邮件或做破坏性批量修改。

4. ClickHouse 查询必须安全。
   SQL 必须经过 validator，只允许 `SELECT`，必须包含项目、玩家标识、时间范围、白名单表/字段和 `LIMIT`。

5. 模型负责业务判断。
   邮件分类、是否查日志、如何生成 SQL、可信度判断、使用哪个标签、是否创建草稿或转人工，都由模型通过工具调用完成。

6. 终端输出保持简短。
   终端只显示模型/工具运行状态和最终自然语言结论，不输出 token、完整邮件正文、完整 prompt、完整工具 JSON 或大量 SQL 结果。

## 3. 当前架构

```text
人工主动对话
  terminal_chat / chat_server
    -> SupportAgentRunner
      -> Forge WorkflowRunner
        -> 本地模型
        -> Gmail / ClickHouse / Rules / Templates / State / Notify tools
        -> respond 自然语言回复

自动处理
  auto_worker / manual_trigger
    -> Gmail 候选 ID 发现
    -> ProcessedMessageStore 去重
    -> build_auto_task 自然语言任务
    -> SupportAgentRunner
      -> Forge WorkflowRunner
        -> 本地模型
        -> 工具调用
        -> save_case_state
    -> 记录状态 / 失败转人工
```

## 4. 工作模式

### 模式 A：人工主动对话

目标：用户可以在终端或 Web UI 中用自然语言询问和控制 Agent。

典型问题：

- 目前所有未读邮件有哪些？
- 总结每个未读邮件里用户表达的主题。
- 查看所有项目未读玩家反馈。
- 帮我分析 BlackHole 标签下最新一封邮件。
- 查一下某个玩家购买不到账是否可信。
- 处理指定 message_id/thread_id 的邮件。

要求：

- 入口层只调用 `agent.run(user_input)`。
- 模型自行决定是否调用 Gmail、ClickHouse、规则库、模板库、状态或通知工具。
- 最终输出必须是模型整理后的自然语言结论。
- dry-run 下不真实写 Gmail；live 必须显式确认。

### 模式 B：自动定时处理

目标：系统定时发现新增玩家反馈邮件，交给模型处理。

流程：

1. Scheduler/Worker 定时触发。
2. Gmail 只读发现未读候选邮件 ID。
3. `ProcessedMessageStore` 过滤已处理、处理中或超过重试上限的邮件。
4. 没有新邮件时记录 `skipped`，不调用模型。
5. 有新邮件时构造自然语言任务。
6. 调用统一的 `SupportAgentRunner`。
7. 模型通过工具读取邮件、判断项目/类型、查日志、打标签、创建草稿或转人工。
8. 模型必须调用 `save_case_state`。
9. Worker 根据模型保存的状态记录处理结果。

Scheduler/Worker 不允许：

- 判断邮件类型。
- 判断是否查 ClickHouse。
- 生成 SQL。
- 判断玩家反馈可信度。
- 选择业务标签。
- 创建草稿。
- 决定是否转人工。

## 5. 主要模块职责

| 模块 | 职责 |
| --- | --- |
| `agent_runner.py` | 封装本地模型客户端、Forge `WorkflowRunner`、状态输出、互动记忆。 |
| `workflows.py` | 构建自动处理 workflow 和主动对话 workflow。 |
| `prompts.py` | 定义模型运行时策略、工具使用规则和安全边界。 |
| `auto_worker.py` | 自动发现候选邮件、去重、构造任务、调用模型、记录结果。 |
| `manual_trigger.py` | 手动模拟 Gmail 发现到模型处理的测试流程。 |
| `terminal_chat.py` | 终端主动对话入口。 |
| `chat_server.py` | 本地 Web 控制 UI。 |
| `processed_message_store.py` | 自动处理和主动对话状态记录。 |
| `tools/gmail_tools.py` | Gmail 只读、已有标签应用、草稿创建。 |
| `tools/clickhouse_tools.py` | ClickHouse schema、SQL validator、只读查询和日志摘要。 |
| `tools/rule_tools.py` | 规则库、知识库、回复模板读取。 |
| `tools/decision_tools.py` | 结构化问题提取、身份字段检查、可信度和动作决策结构化。 |
| `tools/notify_tools.py` | 文件、Webhook、飞书、SMTP 等人工通知。 |
| `tools/state_tools.py` | 模型可调用的 case 状态保存和审计记录。 |
| `readiness_check.py` | 运行前检查模型、Gmail、ClickHouse、状态路径和配置。 |

## 6. 工具能力边界

### Gmail 工具

允许：

- 搜索邮件。
- 发现项目标签下未读邮件。
- 读取指定线程。
- 读取已有 Gmail 标签。
- 应用已有且已校验的 Gmail 标签。
- 创建 Gmail 回复草稿。

禁止：

- 创建新 Gmail 标签。
- 发送邮件。
- 删除、归档、移动到垃圾箱。
- 修改 OAuth 配置。
- 输出 token、refresh token、client secret。

### ClickHouse 工具

允许：

- 读取项目/问题类型对应的白名单表结构。
- 校验 SQL。
- 执行只读 `SELECT` 查询。
- 返回紧凑结果摘要。

禁止：

- 写入或修改数据。
- 查询未配置项目的日志。
- 查询未在白名单中的表或字段。
- 返回大量原始日志给终端。

### 人工客服工具

允许：

- 生成 compact handoff summary。
- 通过文件、Webhook、飞书或 SMTP 通知人工。

要求：

- 工具失败时记录失败状态。
- 自动处理失败时必须进入人工通知路径或保留可追踪文件记录。

## 7. 多项目处理方案

Gmail 父标签作为项目名，例如：

- `NumberCrush`
- `BlackHole`
- `BusFever`
- `Tile Block Jam`
- `Grill Master`

多项目设计：

- Worker 可把 Gmail 父标签作为自然语言任务中的 project hint。
- 模型必须通过 `read_email_thread` 和 `get_existing_gmail_labels` 校验项目。
- 项目专属规则通过 `[knowledge.project_rules_paths]` 配置。
- 项目专属模板通过 `[knowledge.project_templates_dirs]` 配置。
- 项目专属 ClickHouse 表通过 `project_case_type_tables` 配置。
- 如果项目没有表映射，ClickHouse 工具 fail closed，不回退到其他项目表。

## 8. 状态管理

自动处理状态至少记录：

- `message_id`
- `thread_id`
- `project_label`
- `matched_labels`
- `first_seen_at`
- `last_processed_at`
- `status`
- `agent_run_id`
- `draft_id`
- `labels_applied`
- `error_message`
- `retry_count`

状态类型：

- `pending`
- `processing`
- `draft_created`
- `human_review`
- `failed`
- `skipped`
- `discovery_only`

规则：

- Gmail UNREAD 为是否仍需处理的真相源；本地 terminal 状态不能跳过仍为 UNREAD 的邮件。
- 失败邮件可按重试上限重试。
- 模型没有调用 `save_case_state` 时，Worker 标记为 `failed`。
- **缓解**：`create_gmail_draft` 成功后 `agent_runner` 可自动写入 `save_case_state(draft_created)`。
- 主动对话也记录 run 状态，但只保存安全摘要。

## 9. 当前运行路径

### 启动本地模型

```bash
/opt/homebrew/bin/llama-server \
  -m "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --jinja \
  -ngl 999 \
  --host 127.0.0.1 \
  --port 8080
```

### Readiness 检查

```bash
.venv/bin/python -m examples.player_support_agent.auto_worker \
  --config examples/player_support_agent/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1 \
  --readiness-check \
  --readiness-include-discovery
```

### 自动处理 dry-run

```bash
.venv/bin/python -m examples.player_support_agent.auto_worker \
  --config examples/player_support_agent/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1 \
  --max-candidates 20 \
  --max-new 1
```

### 主动对话

```bash
.venv/bin/python -m examples.player_support_agent.terminal_chat \
  --config examples/player_support_agent/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1
```

## 10. 分阶段计划

### 阶段 1：安全基线

目标：确认 Gmail、ClickHouse、模型和状态路径安全可用。

验收：

- Gmail refresh-token 可用。
- ClickHouse 远程连接可用。
- SQL validator 可拦截危险 SQL。
- Gmail 工具无发送、删除、创建标签能力。
- readiness check 可输出安全摘要。

### 阶段 2：自动处理 dry-run

目标：跑通 Gmail 发现、去重、模型处理、状态记录、失败转人工。

验收：

- 无新邮件时不调用模型。
- 有新邮件时调用模型。
- 模型保存 case 状态。
- dry-run 不真实写 Gmail。
- 失败可追踪并进入人工通知路径。

### 阶段 3：主动对话增强

目标：人工可以通过终端/Web 自然语言询问邮件概况、未读邮件、主题总结、指定邮件分析和日志核验。

验收：

- 所有主动输入仍走 `SupportAgentRunner`。
- 未读邮件列表和主题总结由模型通过 Gmail 工具完成。
- 入口层没有关键词直连 Gmail/ClickHouse。
- 终端只显示简短状态和自然语言结果。

### 阶段 4：多项目知识库完善

目标：每个项目拥有可维护的规则、模板、ClickHouse 表映射和标签策略。

验收：

- 每个 Gmail 父标签项目都有规则覆盖。
- 常见问题类型有项目内标签候选。
- 需要查日志的问题有项目表映射。
- 没有映射时安全转人工，不跨项目查询。

### 阶段 5：小流量 live 草稿

目标：在确认 dry-run 稳定后，小批量允许创建草稿和应用已有标签。

验收：

- `--live --max-new 1` 可控运行。
- Gmail 只创建草稿，不发送。
- 草稿在原邮件线程内。
- 状态记录 draft_id 和 labels_applied。
- 高风险 case 转人工。

### 阶段 6：定时化和运营监控

目标：稳定运行定时自动处理，支持人工可观测和回溯。

验收：

- 周期 worker 可运行。
- 有失败/人工转交通知。
- 有审计日志和处理统计。
- 可按项目、问题类型、状态统计处理结果。

## 11. 验收标准

整体项目达到可用状态时，应满足：

- 主动对话和自动处理都经过本地模型与 Forge WorkflowRunner。
- Gmail 不具备发送邮件、删除邮件、创建标签能力。
- ClickHouse 查询安全闭环有效。
- 模型最终输出自然语言总结。
- 自动处理可去重、重试、失败转人工。
- 多项目标签、规则、模板和日志查询能按项目隔离。
- 所有关键路径有单测覆盖。

