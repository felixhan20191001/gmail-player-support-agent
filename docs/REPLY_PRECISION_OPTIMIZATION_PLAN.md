# 邮件回复精准度优化方案

最后更新：2026-06-02

本文档用于指导后续 AI 或 coding agent 优化
`examples/player_support_agent` 的邮件回复质量。目标不是让入口层变聪明，
而是让模型在 Forge 工具链里拿到更完整、更结构化、更可验证的证据。

## 1. 目标

让本地模型和云模型在处理 Gmail 玩家反馈时：

- 更准确识别项目、问题类型、玩家诉求和回复语言。
- 更稳地使用项目规则、回复模板和 ClickHouse 行为日志。
- 减少泛泛回复、错误承诺、错误项目标签、错误日志查询和过度自信。
- 对支付、退款、补偿、账号安全、证据矛盾等高风险 case 稳定转人工。
- 保持当前安全边界：只创建 Gmail 草稿，不发送邮件。

## 2. 不可破坏的架构边界

这些约束优先级高于任何“提升精准度”的尝试：

- 用户主动请求必须进入 `SupportAgentRunner`，由 Forge `WorkflowRunner`
  驱动模型和工具调用。
- CLI/Web 入口不得用关键词、正则或 if/else 直接决定 Gmail、ClickHouse、
  SQL、标签、草稿或转人工动作。
- 自动 worker 只能直接做 Gmail 候选 ID 发现、去重、构造自然语言任务，
  然后调用共享 agent runner。
- Gmail 工具不得发送、删除、归档、移动到垃圾箱或创建标签。
- Gmail 工具只能读取邮件、读取已有标签、应用已存在且通过校验的标签、
  创建草稿。
- ClickHouse SQL 必须经过 validator，只允许 `SELECT`，必须有项目、玩家
  标识、时间范围、白名单表/字段和 `LIMIT`。
- 如果模型传入的项目没有 ClickHouse 表映射，必须 fail closed，不能回退到
  全局表或其他项目表。
- 不得记录 OAuth token、refresh token、client secret、完整私密邮件正文、
  完整 prompt 或大批量原始日志。
- 如果某项行为必须影响模型运行时决策，要修改 `prompts.py`、规则、模板、
  工具 schema 或工具返回内容；不要依赖 Codex 文档作为运行时策略。

## 3. 当前诊断

当前主链路是正确的：

```text
terminal_chat / chat_server / auto_worker
  -> SupportAgentRunner
  -> Forge WorkflowRunner
  -> selected model runtime
  -> Gmail / ClickHouse / rules / templates / state / notify tools
```

主要精度短板在信息供给和证据结构，而不是入口层：

- 多项目覆盖不均衡：Gmail 项目标签很多，ClickHouse 也已经配置了多个项目表，
  但项目级规则和模板目前主要覆盖 `NumberCrush`。
- 规则匹配偏关键词包含：`get_relevant_support_rules` 主要依赖
  `case_type + triggers`，玩家换一种表达方式时容易漏掉具体规则。
- 决策工具偏“规范化”：`assess_claim_credibility` 和 `decide_support_action`
  接收模型给出的 verdict、confidence 和 evidence，再按阈值处理；工具本身
  还没有强校验证据是否真的支持结论。
- ClickHouse 查询安全性够，但证据语义不够：工具返回 event counts 和 sample
  rows，模型还需要自己理解哪些事件能证明支付成功、广告类型、道具消耗、
  复活或奖励领取。
- 草稿缺少独立质检：模型生成草稿后直接创建 draft，缺少一个结构化检查环节来
  拦截语言错误、承诺过度、证据不一致和遗漏字段。
- 云模型切换已经可用，但当前是“整个 runner 换模型”。复杂 case 更稳的做法是
  增加云模型顾问工具，而不是让云模型绕过主 workflow 直接写 Gmail。

## 4. 优化原则

优先补“可调用知识”和“证据摘要”，再调 prompt。

推荐顺序：

1. 先让模型拿到项目画像、标签 taxonomy、商品/广告/存档/玩法规则。
2. 再让模型拿到更确定的日志证据，而不是自己从原始行里猜。
3. 然后补草稿质检，限制错误承诺和语言漂移。
4. 最后再用 prompt 调整工具调用顺序和措辞风格。

所有新增能力都应设计为工具、配置、规则、模板或测试，而不是入口层逻辑。

## 5. 目标工作流

建议把邮件处理流程调整为：

```text
1. read_email_thread
2. get_existing_gmail_labels
3. infer project from Gmail labels and task hints
4. get_project_support_profile(project)
5. extract_feedback_claim(project, available_label_names, detected_language)
6. get_relevant_support_rules(project, case_type, email_text)
7. resolve_player_identity
8. plan_support_evidence / query_support_evidence if logs are needed
9. assess_claim_credibility with structured evidence ids
10. decide_support_action with rule ids and evidence status
11. get_reply_template when rule has template
12. draft_player_reply
13. review_reply_draft
14. create_gmail_draft or create_human_handoff_summary + notify_human_support
15. apply_existing_gmail_labels after decision
16. save_case_state and write_audit_log
```

关键调整：

- `apply_existing_gmail_labels` 应放在证据和动作决策之后。
- `create_gmail_draft` 前增加草稿质检。
- 日志查询尽量从自由 SQL 走向配置化 evidence recipe。

## 6. 需要补充的内容

### 6.1 项目支持画像

为每个重点项目补一个 project profile。建议字段：

```toml
project = "NumberCrush"
aliases = ["Number Crush", "NumberCrush"]

[labels]
allowed_issue_suffixes = [
  "广告问题",
  "bug反馈",
  "崩溃卡死",
  "存档丢失",
  "内购问题",
  "去广告后有广告",
  "咨询",
  "其他",
]

[policy]
supports_cloud_save = false
supports_purchase_restore = false
remove_ads_policy = "Removes forced/interstitial ads; rewarded ads may remain optional."
save_policy = "Progress is local unless the project explicitly supports cloud save."

[[products]]
kind = "pass"
product_id_contains = ["pass"]
reply_policy = "Pass rewards unlock by stage or progress, not all at once."

[[known_issues]]
id = "daily_puzzle_gray_square"
case_types = ["bug"]
triggers = ["daily puzzle", "gray square", "blank puzzle"]
requires_logs = false
safe_reply = "Ask for screenshot, date, app version, and user id; do not promise compensation."

[forbidden_promises]
items = [
  "Do not promise refunds.",
  "Do not promise item compensation.",
  "Do not say a draft has been sent.",
]
```

实现建议：

- 新增 `KnowledgeConfig.project_profiles_paths` 或 `project_profiles_dir`。
- 在 `RuleTools` 中新增 `get_project_support_profile(project)`。
- 在 `forge_tools.py` 注册工具。
- 在 `prompts.py` 要求模型推断项目后先读取 profile。
- 测试覆盖：存在 profile、缺失 profile、跨项目 profile 不回退。

### 6.2 项目级规则和模板

当前通用规则可以保留，但高频项目应有项目级规则文件。

优先补这些类型：

- `payment`
- `ads_after_purchase`
- `lost_save`
- `crash_or_freeze`
- `bug`
- `gameplay_misunderstanding`
- `feature_request`
- `general_question`

每条规则建议补齐：

- `id`
- `projects`
- `case_types`
- `triggers`
- `summary`
- `requires_logs`
- `required_evidence`
- `condition`
- `action`
- `human_review`
- `reply_template`
- `instructions`
- `log_query` 或 `evidence_recipe`

模板至少补：

- `zh-CN`
- `en`

如果暂时没有多语言模板，模型可以用模板作为事实骨架，但必须按
`detected_language` 改写草稿。

### 6.3 标签 taxonomy

当前 `label_suffix_by_case_type` 可生成项目本地子标签候选，但需要确认每个项目
是否真的存在对应 Gmail 子标签。

建议新增只读检查命令或 readiness 子检查：

```text
project -> case_type -> expected label -> exists in Gmail? -> usable?
```

输出用于人工补 Gmail 标签或调整配置。工具层仍然不能创建标签。

### 6.4 结构化日志证据

自由 SQL 对本地小模型要求偏高。建议增加 evidence recipe，让模型选择要验证
什么，而不是直接从零写 SQL。

建议新增工具：

```text
plan_support_evidence(project, case_type, claim_summary)
query_support_evidence(project, case_type, player_id, time_window, evidence_kind)
```

`evidence_kind` 示例：

- `purchase_success`
- `pass_purchase`
- `remove_ads_purchase`
- `forced_ad_after_purchase`
- `rewarded_ad_interaction`
- `coin_spend`
- `revive_spend`
- `item_gain`
- `item_spend`
- `level_crash_context`
- `session_before_report`

工具内部根据配置生成 SQL，再调用现有 validator 和 query。

返回给模型的结果应类似：

```json
{
  "evidence_kind": "pass_purchase",
  "status": "supported",
  "confidence": 0.92,
  "facts": [
    "Found PaySuccess event for product_id=season_pass_01.",
    "No refund or failure event found in the checked window."
  ],
  "missing_data": [],
  "sql_used": "SELECT ... LIMIT 100"
}
```

注意：

- 仍然保留 `validate_clickhouse_sql` 和 `query_clickhouse`，作为 fallback 或人工调试。
- 对自动处理，优先让模型调用 evidence 工具。
- SQL 结果继续只返回 compact summary，不回传大批量原始行。

### 6.5 草稿质检

建议新增 `review_reply_draft` 工具。输入：

- `project`
- `case_type`
- `detected_language`
- `claim_summary`
- `matched_rule_ids`
- `evidence_summary`
- `decision`
- `draft_body`

输出：

```json
{
  "ok": true,
  "risk_level": "low",
  "issues": [],
  "required_fixes": [],
  "safe_to_create_draft": true
}
```

必须检查：

- 是否使用玩家语言。
- 是否声称邮件已发送、问题已解决、补偿已发放或退款已完成。
- 是否承诺未来功能发布时间。
- 是否在证据不足时下确定结论。
- 是否遗漏必要字段，如 user id、订单号、截图、发生时间、设备/版本。
- 是否和 `decide_support_action` 的结论冲突。
- 是否对高风险 case 直接承诺处理。

`create_gmail_draft` 应增加 prerequisite：必须先通过 `review_reply_draft`。

### 6.6 云模型顾问

当前 Web UI 已支持本地/云模型 runtime 切换。后续可以增加一个云模型顾问工具，
让本地模型在复杂 case 中调用云模型给建议。

建议工具名：

```text
get_cloud_support_plan
```

输入只允许脱敏和摘要内容：

- project
- case_type
- detected_language
- claim_summary
- relevant rule summaries
- structured evidence summary
- missing fields
- draft candidate 可选

输出结构化建议：

```json
{
  "recommended_action": "draft_missing_info",
  "confidence": 0.78,
  "risk_flags": ["payment", "missing_order_id"],
  "reply_points": [
    "Acknowledge the payment issue.",
    "Ask for order id and screenshot.",
    "Do not promise refund or activation."
  ],
  "handoff_reason": null
}
```

边界：

- 云模型不得直接调用 Gmail、ClickHouse 或 state tools。
- 云模型不得接收 OAuth token、完整邮件线程或大批量日志。
- 最终决策和写操作仍由主 WorkflowRunner 通过本地工具完成。
- 可以加缓存，按 `project + case_type + normalized_claim + evidence_hash`
  复用建议。

## 7. 分阶段实现流程

### Phase 0：覆盖率盘点

目标：知道每个项目缺什么。

实现：

- 新增只读 coverage 脚本或工具，输出：
  - Gmail project label
  - ClickHouse table mapping
  - project profile
  - project rules path
  - project templates dir
  - expected labels existing in Gmail
- 不调用模型，不处理邮件。

验收：

- 运行后能看到哪些项目只有日志映射、没有规则或模板。
- 输出不包含 token、私密邮件正文或 secrets。

### Phase 1：项目画像工具

目标：模型先拿项目事实，再处理邮件。

实现：

- 增加 profile 配置。
- 增加 `get_project_support_profile` 工具。
- 修改 prompt 和 auto task，要求推断项目后调用 profile。
- 为 `NumberCrush` 和 1-2 个高频项目先写 profile。

验收：

- 单测覆盖 profile 读取、缺失 profile、安全 fallback。
- 模型 dry-run 中能在分类前调用 profile。

### Phase 2：规则和模板扩展

目标：减少通用回复。

实现：

- 给高频项目补项目级规则文件。
- 给高频 case 补 `zh-CN` 和 `en` 模板。
- 把常见误解和项目规则从 prompt 下沉到规则和 profile。

验收：

- `get_relevant_support_rules(project=...)` 返回项目专属规则。
- 模板缺失时模型能用玩家语言自行写，不报错卡住。

### Phase 3：结构化日志证据

目标：减少模型乱写 SQL 和误读日志。

实现：

- 增加 evidence recipe 配置。
- 增加 `plan_support_evidence` 和 `query_support_evidence`。
- evidence 工具内部生成 SQL 并复用现有 validator。
- prompt 中要求优先调用 evidence 工具。

验收：

- 支付、去广告、pass、道具消耗、复活至少各有一个 recipe。
- 项目无 recipe 或无表映射时 fail closed，并转人工或问缺失信息。

### Phase 4：草稿质检

目标：在创建 Gmail draft 前拦截不安全回复。

实现：

- 增加 `review_reply_draft` 工具。
- `create_gmail_draft` prerequisite 依赖 draft review。
- prompt 要求质检失败时修订草稿或转人工。

验收：

- 单测覆盖错误承诺、语言错误、缺字段、证据冲突。
- live 前 dry-run 中能看到 review 通过结果。

### Phase 5：云模型顾问

目标：复杂 case 提升推理质量，但不放大写操作风险。

实现：

- 增加云模型配置，复用 `ModelConfig` 或新增 adviser config。
- 增加 `get_cloud_support_plan` 工具。
- 对输入做脱敏和长度限制。
- 给工具加缓存和超时。

验收：

- 云模型不可直接写 Gmail。
- 云模型不可看到 secrets。
- 云模型失败时主模型可以继续转人工，而不是整体失败。

### Phase 6：评测集和回归

目标：后续每次改 prompt/规则/工具都能知道有没有退化。

实现：

- 建立脱敏样例集：
  - email subject/body/labels
  - expected project
  - expected case_type
  - expected missing_fields
  - expected action
  - expected labels
  - expected reply points
- 增加离线 eval runner，不访问真实 Gmail，不写真实状态。

验收：

- 至少 20-50 条真实脱敏样例。
- 本地模型和云模型都能跑同一套样例。
- 输出准确率和失败原因摘要。

## 8. 优先级

### P0

- 保持入口层模型驱动，不新增业务关键词路由。
- 保持 Gmail 和 ClickHouse 安全边界。
- 新增 coverage 报告，明确项目规则、模板、日志和标签缺口。
- 把 `apply_existing_gmail_labels` 调整到决策后执行。

### P1

- 增加 `get_project_support_profile`。
- 为高频项目补 project profile。
- 为支付、去广告、pass、存档丢失、bug 补项目规则和模板。
- 增加结构化 evidence recipe。
- 增加 `review_reply_draft`。

### P2

- 增加云模型顾问工具。
- 增加多语言模板覆盖。
- 增加 case replay 和处理报表。
- 建立自动化评测集。

## 9. 推荐测试命令

改动 player support agent 后，至少运行：

```bash
.venv/bin/python -m compileall examples/player_support_agent
.venv/bin/python -m pytest \
  tests/unit/test_player_support_entrypoints.py \
  tests/unit/test_player_support_config.py \
  tests/unit/test_player_support_gmail.py \
  tests/unit/test_player_support_clickhouse.py \
  tests/unit/test_support_rules.py \
  tests/unit/test_player_support_decisions.py
```

如果改到 Forge 共享 runner、client、context 或 response validator，再运行相关
Forge 单测。

## 10. 后续 AI 接手提示

开始实现前先读：

- `AGENTS.md`
- `examples/player_support_agent/SKILL.md`
- `examples/player_support_agent/docs/PROJECT_PLAN.md`
- `examples/player_support_agent/docs/OPTIMIZATION_BACKLOG.md`
- 本文档

改动位置优先级：

- 模型运行时行为：`prompts.py`、规则、模板、工具 schema、工具描述。
- 项目知识：`knowledge/`、`templates/`、`tools/rule_tools.py`、config 中的
  `[knowledge]`。
- 证据查询：`tools/clickhouse_tools.py` 和 ClickHouse config。
- Gmail 行为：`tools/gmail_tools.py`，但不得新增发送、删除、归档、创建标签。
- 自动处理：`auto_worker.py` 和 `auto_task_builder.py`，但 worker 不得做业务判断。

一个实用落地顺序：

1. 先做 coverage 报告。
2. 给 `NumberCrush` 完整跑通 profile + evidence + draft review。
3. 复制模式到 2-3 个高频项目。
4. 加脱敏 eval。
5. 再考虑云模型顾问。
