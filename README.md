# 多项目玩家客服 Agent

基于 `forge-guardrails` 包构建的独立 Gmail 玩家客服 Agent。
它使用本地或 OpenAI 兼容模型对多个项目的 Gmail 邮件线程进行分类，
查询项目专属的 ClickHouse 日志，并创建 Gmail 回复草稿或转人工处理。

## 安装

```bash
cd /Users/hanpengfei/Documents/repo/gmailAgent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Forge 作为普通依赖从 PyPI 安装（`import forge`），不需要本地 Forge 源码。

## 当前能力范围

- Gmail 父标签作为项目名，例如 `NumberCrush`、`BlackHole`、`BusFever`。
- 仅应用已存在的 Gmail 标签，绝不创建新标签。
- 仅创建 Gmail 草稿，绝不发送邮件。
- Dry-run 模式下阻止 Gmail 写入、状态写入和通知。默认允许只读 ClickHouse 查询用于模型取证，除非传入 `--block-db-in-dry-run`。

## 配置

本地配置文件被 Git 忽略：

```text
config/config.local.toml
```

持久化 Gmail 认证推荐使用 refresh-token 环境变量：

```bash
export GOOGLE_CLIENT_ID="..."
export GOOGLE_CLIENT_SECRET="..."
export GMAIL_REFRESH_TOKEN="..."
```

快速测试也可以使用短期 access token：

```bash
export GMAIL_ACCESS_TOKEN="..."
```

模型运行时可在 `config.local.toml` 中配置：

```toml
[model]
backend = "llamaserver"
gguf_path = "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf"
base_url = "http://localhost:8080/v1"

# 或使用提供 /v1/chat/completions 的云服务 API：
# backend = "openai-compatible"
# model = "your-cloud-model"
# base_url = "https://api.openai.com/v1"
# api_key_env = "OPENAI_API_KEY"
```

`--backend`、`--model`、`--base-url`、`--api-key-env`、`--gguf` 等 CLI 参数可覆盖单次运行的配置。API Key 优先使用环境变量或密钥文件，避免密钥出现在 shell 历史中。

## 预检查

预检查不调用模型：

```bash
cd /Users/hanpengfei/Documents/repo/gmailAgent
source .venv/bin/activate

player-support-preflight --config config/config.local.toml
```

## 就绪检查

试运行自动处理前，先运行就绪检查。它会验证本地配置、可写状态路径、模型配置、Gmail 已有标签和 ClickHouse 配置的表/列。对于本地模型后端，检查本地 `/models` 端点；对于云端后端，检查 API Key 是否已实际配置。输出简洁的安全报告，不处理邮件。

```bash
player-support-readiness --config config/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1
```

添加 `--readiness-include-discovery` 可在不调用模型的情况下测试 Gmail 未读项目候选发现。如果报告为 `BLOCKED`，先修复这些问题再运行 worker。如果警告是预期的（例如某个项目没有日志表映射），`READY_WITH_WARNINGS` 可用于 dry-run 试运行。

## 本地模型

使用 LM Studio 将 GGUF 模型下载到 `/Users/hanpengfei/models` 后，找到模型文件：

```bash
find /Users/hanpengfei/models -name "*.gguf" -type f
```

启动 `llama-server`：

```bash
llama-server \
  -m "/Users/hanpengfei/models/path/to/model.gguf" \
  --jinja \
  -ngl 999 \
  --port 8080
```

运行一次模型驱动的请求：

```bash
player-support-chat --config config/config.local.toml \
  --backend llamaserver \
  --gguf "/Users/hanpengfei/models/path/to/model.gguf" \
  --ask "查看所有项目未读玩家反馈"
```

## 云端模型

当服务提供商提供支持原生工具调用的 OpenAI 兼容 `/v1/chat/completions` API 时使用：

```bash
export OPENAI_API_KEY="..."

player-support-chat --config config/config.local.toml \
  --backend openai-compatible \
  --model "your-cloud-model" \
  --base-url https://api.openai.com/v1
```

所有 Gmail、ClickHouse、标签、草稿和转交行为仍通过相同的 Forge 工具和安全检查。云端模式仅更换模型客户端。

## 本地 Web 控制界面

内置的 llama.cpp `llama-ui` 可以与模型聊天，但无法执行 Gmail、ClickHouse、规则、标签或草稿等 Forge 工具。如需文本化控制，启动 Forge 控制 UI：

```bash
player-support-web --config config/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1
```

然后打开：

```text
http://127.0.0.1:8090
```

界面包含以下按钮：

- 切换已配置的模型 profile；
- 在本地被忽略的 `var/` 密钥文件中保存或切换命名的云端模型 API Key；
- 预检查和就绪检查；
- 仅 Gmail 发现测试；
- 通过自动 worker 路径的一次性 dry-run 处理；
- 显式确认后创建正式 Gmail 草稿。

要同时显示本地和云端模型按钮，在 `config.local.toml` 中添加可选 profile：

```toml
[model_profiles.local]
backend = "llamaserver"
gguf_path = "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf"
base_url = "http://localhost:8080/v1"
llamafile_mode = "prompt"

[model_profiles.cloud]
backend = "openai-compatible"
model = "your-cloud-model"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
```

模型切换在进程内生效，影响下一次 Web UI 请求。UI 绝不打印 API Key 或 token 值。`云 Key` 按钮将密钥存储在 `var/player_support_agent/cloud_model_keys/` 下（被 Git 忽略）；在对话框中留空密钥值可切换到已保存的密钥名称。

常用提示词：

```text
目前所有未读邮件有哪些
总结每个未读邮件里用户表达的主题
查看所有项目未读玩家反馈
帮我分析 BlackHole 标签下最新一封邮件
处理 thread_id=... message_id=...
确认正式处理 1 封邮件
```

控制 UI 默认为 dry-run。仅当聊天消息包含精确短语 `确认正式处理`，或 live 手动运行按钮在其提示中收到相同确认时，才会运行正式 Gmail 写入路径。正式路径可创建 Gmail 草稿和应用已有标签，但仍不支持 Gmail 发送。

## 终端对话

如果想在终端中使用模型驱动的自然语言控制：

```bash
player-support-chat --config config/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1
```

终端入口将每个问题先发给本地模型，模型再决定是否调用 Gmail、ClickHouse、客服规则、回复模板或不调用工具。终端在工具运行时只打印简洁的状态行，然后打印模型的最终自然语言回答。

一次性模式适用于快速检查：

```bash
player-support-chat --ask "查看所有项目未读玩家反馈"
```

## 自动 Worker

自动 worker 是唯一在模型执行前直接获取 Gmail 候选 ID 的入口。它发现已有 Gmail 项目标签下的未读收件箱邮件，通过已处理消息存储去重，构建包含项目标签提示的自然语言任务，然后调用与终端和 Web UI 相同的模型驱动 Agent 运行器。

项目专属行为由模型/工具驱动：

- Gmail 父标签标识项目。
- `label_suffix_by_case_type` 可推荐项目内标签候选，例如 `BlackHole/内购问题`；Gmail 工具仍会在应用前校验标签存在。
- `get_relevant_support_rules(project=...)` 在配置了项目专属规则时选择之，否则回退到默认规则文件。
- `get_clickhouse_schema(project=..., case_type=...)` 选择项目专属的白名单表。当项目明确但不存在表映射时，工具不返回任何允许的表，而不是回退到另一个项目的日志。
- 启用 `require_project_for_queries = true` 时，ClickHouse 工具还会拒绝无项目的日志查询，因此模型必须先推断出项目再查询日志。
- 模型仍决定是否需要日志、校验哪条 SQL、应用哪个已有标签、是起草回复还是转人工。

对于每封选中的 Gmail 邮件，模型必须在最终回答前调用 `save_case_state`。Worker 将每封邮件的结果记录为 `draft_created`、`human_review`、`failed` 或 `skipped`；如果模型没有为选中邮件保存结果，该邮件将被标记为 `failed`，以便重试或调查，而不是被静默地视为已处理。失败的自动处理 case 通过配置的通知方式升级。默认 `file` 模式将转交笔记写入 `var/player_support_agent/handoffs`；设置 `[notify].mode = "feishu"` 和 `feishu_webhook_url` 可将相同简洁的失败摘要推送到飞书机器人。终端输出保持简洁，仅显示高层的模型/工具/转交状态。

dry-run 模式下运行一次：

```bash
player-support-worker --config config/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1 \
  --max-candidates 20 \
  --max-new 5
```

循环运行：

```bash
player-support-worker --config config/config.local.toml \
  --interval-seconds 300
```

仅当你希望 worker 创建 Gmail 草稿、应用已有标签并写入处理状态时，才添加 `--live`。不支持 Gmail 发送。

## 手动触发

用于一次性测试「Gmail 发现 → 模型处理」流程。它默认使用独立的测试存储，不会污染正常的自动处理消息存储：

```bash
player-support-manual --config config/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1 \
  --max-candidates 20 \
  --max-new 1
```

常用测试参数：

```bash
# 仅检查 Gmail 发现是否找到候选；不调用模型。
--discovery-only

# 即使手动测试存储已标记完成，也重新运行发现的候选。
--ignore-store

# 测试自定义 Gmail 查询，例如某个项目标签。
--query 'is:unread in:inbox -in:spam -in:trash label:"BlackHole"'

# 使用真实的自动 worker 状态，而非手动测试存储。
--use-config-store
```
