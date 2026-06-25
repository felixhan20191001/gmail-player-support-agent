# Multi-Project Player Support Agent

Standalone Gmail player-support agent built on the `forge-guardrails` package.
It uses a local or OpenAI-compatible model to classify Gmail threads across
multiple projects, query project-specific ClickHouse logs, and create Gmail
drafts or human handoffs.

## Install

```bash
cd /Users/hanpengfei/Documents/repo/gmailAgent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Forge is installed from PyPI as a normal dependency (`import forge`). You do not
need a local Forge source checkout for this project.

## Current Scope

- Gmail parent labels are treated as project names, such as `NumberCrush`,
  `BlackHole`, or `BusFever`.
- Applies only existing Gmail labels. It never creates labels.
- Creates Gmail drafts only; it never sends mail.
- Dry-run blocks Gmail mutations, state writes, and notifications. Read-only
  ClickHouse queries are allowed by default for model evidence gathering unless
  `--block-db-in-dry-run` is passed.

## Config

Local config is intentionally ignored by Git:

```text
config/config.local.toml
```

For durable Gmail auth, prefer refresh-token env vars:

```bash
export GOOGLE_CLIENT_ID="..."
export GOOGLE_CLIENT_SECRET="..."
export GMAIL_REFRESH_TOKEN="..."
```

For quick tests, a short-lived access token also works:

```bash
export GMAIL_ACCESS_TOKEN="..."
```

Model runtime can be configured in `config.local.toml`:

```toml
[model]
backend = "llamaserver"
gguf_path = "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf"
base_url = "http://localhost:8080/v1"

# Or use a cloud/provider API that exposes /v1/chat/completions:
# backend = "openai-compatible"
# model = "your-cloud-model"
# base_url = "https://api.openai.com/v1"
# api_key_env = "OPENAI_API_KEY"
```

CLI flags such as `--backend`, `--model`, `--base-url`, `--api-key-env`, and
`--gguf` override the config for one run. Prefer env vars or key files for API
keys so secrets do not land in shell history.

## Preflight

Preflight does not call the model:

```bash
cd /Users/hanpengfei/Documents/repo/gmailAgent
source .venv/bin/activate

player-support-preflight --config config/config.local.toml
```

## Readiness Check

Before trial-running automatic processing, run the readiness check. It verifies
local config, writable state paths, model configuration, Gmail existing labels,
and ClickHouse configured tables/columns. For local model backends it checks the
local `/models` endpoint; for cloud backends it checks that an API key is
actually configured. It prints a compact safe report and does not process email.

```bash
player-support-readiness --config config/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1
```

Add `--readiness-include-discovery` to also test Gmail unread-project candidate
discovery without calling the model. If the report is `BLOCKED`, fix those
items before running the worker. `READY_WITH_WARNINGS` can be used for a dry-run
trial if the warnings are expected, such as a project with no log table mapping.

## Local Model

After LM Studio downloads a GGUF under `/Users/hanpengfei/models`, find it:

```bash
find /Users/hanpengfei/models -name "*.gguf" -type f
```

Start `llama-server`:

```bash
llama-server \
  -m "/Users/hanpengfei/models/path/to/model.gguf" \
  --jinja \
  -ngl 999 \
  --port 8080
```

Run a model-driven request:

```bash
python -m examples.player_support_agent.main \
  --config examples/player_support_agent/config.local.toml \
  --backend llamaserver \
  --gguf "/Users/hanpengfei/models/path/to/model.gguf" \
  --ask "查看所有项目未读玩家反馈"
```

## Cloud Model

Use this when the provider exposes an OpenAI-compatible
`/v1/chat/completions` API with native tool calls:

```bash
export OPENAI_API_KEY="..."

.venv/bin/python -m examples.player_support_agent.terminal_chat \
  --config examples/player_support_agent/config.local.toml \
  --backend openai-compatible \
  --model "your-cloud-model" \
  --base-url https://api.openai.com/v1
```

All Gmail, ClickHouse, labeling, draft, and handoff behavior still goes through
the same Forge tools and safety checks. Cloud mode changes only the model
client.

## Local Control UI

The built-in llama.cpp `llama-ui` can chat with the model, but it cannot execute
Forge tools such as Gmail, ClickHouse, rules, labels, or drafts. For text-based
control, start the Forge control UI instead:

```bash
cd /Users/hanpengfei/Documents/repo/forge

.venv/bin/python -m examples.player_support_agent.chat_server \
  --config examples/player_support_agent/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1
```

Then open:

```text
http://127.0.0.1:8090
```

The UI includes buttons for:

- switching between configured model profiles;
- saving or switching named cloud-model API keys in a local ignored `var/`
  secret file;
- preflight and readiness checks;
- Gmail discovery-only testing;
- one-shot dry-run processing through the automatic worker path;
- live Gmail draft creation after the explicit confirmation phrase.

To show both local and cloud model buttons, add optional profiles to
`config.local.toml`:

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

Model switching is in-process and affects the next Web UI request. The UI never
prints API keys or token values. The `云 Key` button stores keys under
`var/player_support_agent/cloud_model_keys/`, which is ignored by Git; leave the
key value blank in that dialog to switch to an already saved key name.

Useful prompts:

```text
目前所有未读邮件有哪些
总结每个未读邮件里用户表达的主题
查看所有项目未读玩家反馈
帮我分析 BlackHole 标签下最新一封邮件
处理 thread_id=... message_id=...
确认正式处理 1 封邮件
```

The control UI defaults to dry-run. It only runs the live Gmail mutation path
when the chat message contains the exact phrase `确认正式处理` or the live
manual-run button receives that same confirmation in its prompt. The live path
can create Gmail drafts and apply existing labels, but Gmail sending is still
not available.

## Terminal Chat

If you want model-driven natural-language control in a terminal, use:

```bash
cd /Users/hanpengfei/Documents/repo/forge

.venv/bin/python -m examples.player_support_agent.terminal_chat \
  --config examples/player_support_agent/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1
```

The terminal entrypoint sends every question to the local model first. The model
then decides whether to call Gmail, ClickHouse, support rules, reply templates,
or no tool. The terminal prints only simple status lines while tools run, then
prints the model's final natural-language answer.

One-shot mode is useful for quick checks:

```bash
.venv/bin/python -m examples.player_support_agent.terminal_chat \
  --ask "查看所有项目未读玩家反馈"
```

## Automatic Worker

The automatic worker is the only entrypoint that directly fetches Gmail
candidate IDs before model execution. It discovers unread inbox messages under
existing Gmail project labels, deduplicates them through the processed-message
store, builds a natural-language task containing project label hints, and then
calls the same model-driven agent runner used by the terminal and Web UI.

Project-specific behavior is model/tool driven:

- Gmail parent labels identify the project.
- `label_suffix_by_case_type` can recommend project-local label candidates such
  as `BlackHole/内购问题`; Gmail tools still validate that the label exists before
  applying it.
- `get_relevant_support_rules(project=...)` selects project-specific knowledge
  when configured, otherwise it falls back to the default rule file.
- `get_clickhouse_schema(project=..., case_type=...)` selects project-specific
  whitelisted tables. When a project is explicit but no table map exists, the
  tool returns no allowed tables instead of falling back to another project's
  logs.
- With `require_project_for_queries = true`, ClickHouse tools also reject
  project-less log queries, so the model must infer the project before querying
  logs.
- The model still decides whether logs are needed, which SQL to validate, which
  existing label to apply, and whether to draft or hand off.

For each selected Gmail message, the model must call `save_case_state` before
its final answer. The worker records those per-message outcomes as
`draft_created`, `human_review`, `failed`, or `skipped`; if the model does not
save an outcome for a selected message, that message is marked `failed` so it
can be retried or investigated instead of being silently treated as processed.
Failed automatic cases are escalated through the configured notification mode.
The default `file` mode writes a handoff note under
`var/player_support_agent/handoffs`; set `[notify].mode = "feishu"` and
`feishu_webhook_url` to push the same compact failure summary to a Feishu bot.
Terminal output stays brief and only shows the high-level model/tool/handoff
status.

Run once in dry-run mode:

```bash
.venv/bin/python -m examples.player_support_agent.auto_worker \
  --config examples/player_support_agent/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1 \
  --max-candidates 20 \
  --max-new 5
```

Run repeatedly:

```bash
.venv/bin/python -m examples.player_support_agent.auto_worker \
  --config examples/player_support_agent/config.local.toml \
  --interval-seconds 300
```

Add `--live` only when you want the worker to create Gmail drafts, apply existing
labels, and write processing state. Gmail sending is not available.

## Manual Trigger

For a one-off test of “Gmail discovery -> model processing”, use the manual
trigger. It defaults to a separate test store, so it will not pollute the normal
automatic processed-message store:

```bash
.venv/bin/python -m examples.player_support_agent.manual_trigger \
  --config examples/player_support_agent/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1 \
  --max-candidates 20 \
  --max-new 1
```

Useful test flags:

```bash
# Only check whether Gmail discovery finds candidates; do not call the model.
--discovery-only

# Re-run a discovered candidate even if the manual test store already marked it done.
--ignore-store

# Test a custom Gmail query, for example one project label.
--query 'is:unread in:inbox -in:spam -in:trash label:"BlackHole"'

# Use the real automatic worker state instead of the manual test store.
--use-config-store
```
