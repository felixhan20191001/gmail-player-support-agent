# Gmail 玩家反馈 AI Agent 待优化项

最后更新：2026-06-17

本文档用于记录当前项目后续需要优化、补齐或重点验证的事项。优先级含义：

- P0：违反核心安全或架构边界，必须立即处理。
- P1：影响稳定性、可观测性、可维护性或上线信心，应尽快处理。
- P2：体验、工程结构或运营效率优化。

## 当前状态摘要

### 整体阶段

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| 阶段 1：安全基线 | 已完成 | Gmail / ClickHouse / validator / readiness 可用 |
| 阶段 2：自动处理 dry-run | 已完成 | 发现、去重、模型处理、状态记录、失败转人工已打通 |
| 阶段 3：主动对话增强 | 基本完成 | terminal / Web 均走 `SupportAgentRunner` |
| 阶段 4：多项目知识库 | 进行中 | 通用规则较全，多数项目仍缺 project profile / 专属规则 |
| 阶段 5：小流量 live 草稿 | 进行中 | live 草稿可创建，但本地模型偶发 `MaxIterationsError` |
| 阶段 6：定时化与运营监控 | 未开始 | 无周期 worker 报表与长期监控 |

### 测试与构建

- 聚焦单测：**207 passed**（`tests/unit/test_player_support_*.py` 等 8 个文件）。
- `compileall src/player_support_agent` 通过。

### 已完成或基本具备

- 本地模型通过 OpenAI-compatible `llama-server` 接入；也支持云模型 profile。
- `SupportAgentRunner` 使用 Forge `WorkflowRunner` 作为主 Agent Loop。
- 主动对话入口：`terminal_chat.py`、`chat_server.py`。
- 自动处理入口：`auto_worker.py`、`manual_trigger.py`。
- 自动 worker 只在发现候选邮件阶段直接调用 Gmail，后续交给模型。
- Gmail 工具限制为读取、已有标签应用和草稿创建；不发送、不删信、不创建标签。
- ClickHouse 工具有 schema、validator、query 和摘要能力；`require_project_for_queries = true`。
- 处理状态、审计和失败转人工路径已具备基础能力。
- readiness check 可检查模型、Gmail、ClickHouse、状态路径和配置。
- UNREAD 为处理真相源：`ProcessedMessageStore.select_candidates_for_run` 统一候选选择。
- Coin Frenzy / 去广告 / 功能建议等通用规则与英文模板已补齐。

### 2026-06-17 本轮修复（MaxIterations / 计时器邮件）

针对 BlackHole 邮件 `19ed15852981a702`（Greg Smith，主题 *Eat Everything*，正文 `ease up with the timer`）反复 `MaxIterationsError` 的修复：

| 问题 | 修复 |
| --- | --- |
| `get_support_evidence_catalog(gameplay_misunderstanding)` 误导向 Coin Frenzy 查库 | 仅 `pass_purchase_misunderstanding` 走 Coin Frenzy 分支；`gameplay_misunderstanding` 走通用 no-recipe 路径 |
| `feature_request` 仍可能触发 ClickHouse | `feature_request` 加入 `skip_log_query_case_types` |
| `get_coin_frenzy_investigation_playbook` 接受 `gameplay_misunderstanding` | 限制为 `pass_purchase_misunderstanding` / `payment` |
| 强匹配 `feature_request_ack`（`requires_logs=false`）后模型仍查库、重读邮件 | `get_relevant_support_rules` 返回明确 `guidance`，要求跳过 ClickHouse 直接起草 |
| 模型创建草稿后未调用 `save_case_state` | `agent_runner.build_message_observer` 在 `create_gmail_draft` 成功后自动 `save_case_state(draft_created)` |
| 草稿已创建但迭代耗尽仍记失败 | `MaxIterationsError` 时若 case 已保存，runner 按成功返回 |

**正确预期行为**（该邮件）：

- `case_type`：`feature_request` 或 `gameplay_misunderstanding`（模型选择）
- 匹配规则：`feature_request_ack`（trigger: `timer` / `ease up`）
- 模板：`templates/replies/en/feature_request_ack.md`
- 标签：`BlackHole` + `BlackHole/功能建议`
- 状态：`draft_created` + `save_case_state`

**待验证**：修复合并后需对 `19ed15852981a702` 再跑一次 `--live --max-new 1`，确认不再转人工。

### 已知风险（仍未关闭）

- 本地小模型仍可能：重读线程、切换 `case_type`、编造其他项目邮件正文（如 BusFever 新手包中文描述）。
- UI 运行摘要中的「玩家反馈」字段来自模型总结，可能与 Gmail 实际正文不一致；以 `chat-*.jsonl` 日志为准。
- 部分历史失败记录在 `var/processed_messages.json` / `var/handoffs/` 中，重跑前 Gmail 仍为 UNREAD 时会自动重选。

### 最近验证过

- 远程 ClickHouse 能连通；白名单表可查询。
- Gmail unread project discovery 能找到候选邮件。
- 自动 live 处理可创建草稿（`draft_id` 形如 `r-*`），但依赖模型走完流程或触发自动 `save_case_state`。
- 运行自动处理前需确保模型服务监听 `http://localhost:8080/v1`（或配置云模型）。

## P0 必须立即处理

| 编号 | 待优化项 | 风险 | 建议动作 |
| --- | --- | --- | --- |
| P0-1 | 主动对话“所有未读邮件/每封邮件主题总结”能力需要完整验证 | 如果工具未完整注册或 prompt 未覆盖，模型可能无法稳定回答未读邮件概况 | 确认 `list_unread_inbox_emails` 或等价只读工具已注册到 workflow，补测试并跑主动对话 dry-run |
| P0-2 | 入口层必须持续禁止关键词直连 Gmail/ClickHouse | 一旦入口层绕过模型，会破坏核心 Agent 架构 | 每次改 `terminal_chat.py`、`chat_server.py`、`main.py`、`gmail_stats.py`、`auto_worker.py` 后检查测试 |
| P0-3 | live 前确认 Gmail 工具没有发送/删除/创建标签能力 | 误发送或破坏邮箱状态 | 保持工具 schema 不暴露 send/delete/archive/create_label；补 schema 级测试 |
| P0-4 | live 前确认 dry-run 包装不会放行 Gmail 写操作 | dry-run 试跑可能真实打标签或建草稿 | 针对 `apply_dry_run` 补 Gmail 写操作模拟测试 |
| P0-5 | ClickHouse 项目路由不能回退到错误项目 | 多项目日志串查会导致错误判断 | 对“项目无映射时 fail closed”补更强测试 |

## P1 应尽快处理

| 编号 | 待优化项 | 影响 | 建议动作 |
| --- | --- | --- | --- |
| P1-1 | 为所有 Gmail 父标签补齐项目配置 | 部分项目需要查日志时会安全失败或转人工 | 给缺失项目补 `project_case_type_tables`、规则和模板，无法查日志的项目明确标注 |
| P1-2 | 完善项目级客服知识库 | 回复策略会偏通用，不能充分体现不同游戏规则 | 为每个项目增加 `project_rules_paths` 和 `project_templates_dirs` |
| P1-3 | 完善常见问题标签映射 | 模型可能只用通用 issue type，无法稳定推荐项目子标签 | 补齐 `label_suffix_by_case_type`，并用 Gmail 现有标签校验 |
| P1-4 | 增强未读邮件主题总结能力 | 仅靠 snippet 可能无法准确总结复杂邮件 | 允许模型按需读取指定 thread；工具返回安全摘要，避免一次性塞入大量正文 |
| P1-5 | 增强状态统计和可观测性 | 后续运营不方便看处理量、失败原因、人工转交量 | 增加按日期/项目/状态统计命令或只读工具 |
| P1-6 | 完善失败转人工通知 | 当前文件通知可用，但飞书/Webhook 需要实测 | 对 Feishu/Webhook 增加端到端 dry-run 或模拟测试 |
| P1-7 | 自动处理重试策略细化 | 同类失败可能重复重试浪费模型调用 | 区分临时失败、配置失败、模型未保存状态、工具权限失败；**部分缓解**：草稿成功后 observer 自动 `save_case_state`，`MaxIterationsError` 时若已保存则不算失败 |
| P1-11 | 本地模型循环与幻觉 | 非购买类邮件误入 Coin Frenzy 路径、编造 email_text、28 轮耗尽 | **部分缓解**：证据目录与规则 guidance 已收紧；仍需 live 回归 `19ed15852981a702` 及更多 timer/feature_request 样例 |
| P1-12 | 运行摘要与邮件正文一致性 | Web/UI 展示模型 hallucinated 摘要 | 摘要应优先来自 `read_email_thread` 安全字段，或标注「模型推断」 |
| P1-8 | 主动对话状态记录进一步结构化 | 只保存 preview 时排查能力有限 | 增加安全 metadata，例如工具名列表、耗时、run status，不保存敏感内容 |
| P1-9 | ClickHouse 查询成本控制 | 多封邮件批量处理可能产生过多日志查询 | 增加每轮最大查询次数、每封邮件最大 SQL 修复次数、超时后的人工策略 |
| P1-10 | 草稿质量验收 | 模型草稿可能过长、语气不一致或承诺不当 | 增加回复模板、禁止承诺补偿/退款、敏感词检查和人工转交规则 |

## P2 后续优化

| 编号 | 待优化项 | 价值 | 建议动作 |
| --- | --- | --- | --- |
| P2-1 | Web UI 展示更清晰的工具状态 | 人工干预时更容易判断模型在做什么 | 展示简短工具状态、最终结果、run_id 和安全错误摘要 |
| P2-2 | 增加只读“邮箱概况”工具 | 用户可问今天/昨天/按项目/按标签的未读分布 | 由模型调用工具生成自然语言报告，不在入口层关键词路由 |
| P2-3 | 增加规则库编辑规范 | 后续补知识库时减少格式漂移 | 给 `knowledge/` 增加 README 和规则字段说明 |
| P2-4 | 增加模板预览工具 | 方便人工检查不同项目/语言模板 | 提供只读模板列表和模板渲染 dry-run |
| P2-5 | 增加 case 回放工具 | 便于复盘某封邮件模型为什么这么处理 | 根据 message_id 读取安全状态、审计和工具摘要 |
| P2-6 | 增加处理报表 | 便于长期运营 | 输出每日处理数、草稿数、人工转交数、失败原因 TopN |
| P2-7 | 增加更细的语言检测 | 邮件可能多语言 | 让模型输出 detected_language，并选择对应模板 |
| P2-8 | 增加附件/截图处理策略 | 玩家可能发截图或 receipt | 先只识别附件存在并转人工，后续再引入 OCR 或附件摘要 |
| P2-9 | 增加部署脚本 | 减少手动命令出错 | 提供 launchd/cron 示例和环境变量检查脚本 |
| P2-10 | 增加模型效果评测集 | 防止 prompt/规则改动导致退化 | 建立匿名样例邮件、期望 issue_type、期望动作和标签 |

## 建议下一步

1. **立即**：对 `mudriderguy@yahoo.com` / `19ed15852981a702` 跑 `--live --max-new 1`，确认 `feature_request_ack` + `draft_created` 成功。
2. 跑一次 readiness check，确认模型、Gmail、ClickHouse 都可用。
3. 补 2–3 条 timer / feature_request 离线 eval 用例，防止规则改动回归。
4. 针对仍高频失败的项目（BlackHole、Number Sum 去广告等）补 project profile 与专属规则。
5. 完成 P0-1：确认主动对话「所有未读邮件/主题总结」工具链路与测试。
6. 扩大 `--live --max-new 1` 到小批量，观察 `var/logs/interactive/` 与 `var/handoffs/`。

## 长期维护规则

- 改入口层时，优先检查是否仍然只调用 `SupportAgentRunner`。
- 改 Gmail 工具时，确认没有新增发送、删除、归档、创建标签能力。
- 改 ClickHouse 工具时，确认 validator 仍然强制只读、项目、玩家、时间和 LIMIT。
- 改模型策略时，同步更新 `prompts.py`、规则库、模板和测试。
- 改自动 worker 时，确认 Scheduler 没有承担业务决策。
- 涉及 live 写 Gmail 前，先 dry-run，再小批量，再扩大。

