---
name: gmail-feedback-agent
description: "Use when working on the multi-project Gmail feedback agent built with a local llama-server model, Forge WorkflowRunner, Gmail project labels, ClickHouse log queries, draft creation, human handoff, or the terminal/Web control UI."
---

# Gmail Feedback Agent

Use this skill when maintaining or operating the local-model + Forge
multi-project support agent in `examples/player_support_agent`.

## Core Shape

- The agent brain is a local OpenAI-compatible model served by `llama-server`.
- The orchestration loop is Forge `WorkflowRunner` through
  `SupportAgentRunner`.
- Entry points should pass natural-language tasks into the agent runner.
- Tools expose Gmail, ClickHouse, support rules, reply templates, state, and
  human handoff.
- Business decisions should stay model-driven through tool calls, not entrypoint
  keyword routing.
- Gmail parent labels are project names. The automatic worker may pass
  project_label/matched_labels hints, but the model must still verify labels
  through Gmail tools.

## Hard Boundaries

- Gmail send/delete/archive/create-label behavior must not be added.
- Gmail write behavior is limited to existing-label application and draft
  creation.
- ClickHouse execution must remain validator-gated, `SELECT` only, scoped, and
  limited.
- Project-specific ClickHouse tables must be selected through tool/config
  `project` routing, not entrypoint if/else logic.
- When a model passes a project to ClickHouse tools, missing project table
  mapping must fail closed instead of falling back to another project's table.
- In multi-project configs, keep `require_project_for_queries = true` unless a
  legacy single-project workflow explicitly needs project-less queries.
- Automatic worker code may directly fetch Gmail candidate IDs only for
  discovery and dedupe.
- Do not print or persist tokens, refresh tokens, client secrets, full prompts,
  full private email bodies, or large raw SQL results.
- Failed automatic message processing must become `failed` state and trigger the
  configured human handoff path.

## Where To Change Things

- Runtime model behavior: `prompts.py`, support rules, reply templates, tool
  descriptions, and tool schemas.
- Entry behavior: `terminal_chat.py`, `chat_server.py`, `auto_worker.py`.
- Shared model loop: `agent_runner.py`.
- Automatic task text: `auto_task_builder.py`.
- Gmail API wrapper: `tools/gmail_tools.py`.
- ClickHouse validation/query behavior: `tools/clickhouse_tools.py`.
- Project-specific knowledge routing: `tools/rule_tools.py` and `[knowledge]`
  config maps.
- Human handoff and Feishu/webhook/file/SMTP notification:
  `tools/notify_tools.py` and `[notify]` config.
- Persistent run/message state: `processed_message_store.py` and
  `tools/state_tools.py`.

## Local Model

Start the model server before running model-driven entrypoints:

```bash
/opt/homebrew/bin/llama-server \
  -m "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --jinja \
  -ngl 999 \
  --host 127.0.0.1 \
  --port 8080
```

The expected healthy signal is that the server reports the model loaded and is
listening on `http://127.0.0.1:8080`.

## Common Commands

Preflight without model:

```bash
.venv/bin/python -m examples.player_support_agent.main \
  --config examples/player_support_agent/config.local.toml \
  --preflight
```

Terminal chat:

```bash
.venv/bin/python -m examples.player_support_agent.terminal_chat \
  --config examples/player_support_agent/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1
```

Web control UI:

```bash
.venv/bin/python -m examples.player_support_agent.chat_server \
  --config examples/player_support_agent/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1
```

Automatic worker readiness check:

```bash
.venv/bin/python -m examples.player_support_agent.auto_worker \
  --config examples/player_support_agent/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1 \
  --readiness-check
```

Use `--readiness-include-discovery` when you also want to verify Gmail unread
project discovery before processing. The readiness check must not process email
or call the model for business decisions.

Automatic worker dry-run:

```bash
.venv/bin/python -m examples.player_support_agent.auto_worker \
  --config examples/player_support_agent/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1 \
  --max-candidates 20 \
  --max-new 5
```

Manual trigger test flow:

```bash
.venv/bin/python -m examples.player_support_agent.manual_trigger \
  --config examples/player_support_agent/config.local.toml \
  --gguf "/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf" \
  --base-url http://localhost:8080/v1 \
  --max-candidates 20 \
  --max-new 1
```

Add `--discovery-only` to test Gmail candidate discovery without calling the
model, or `--ignore-store` to rerun a candidate from the manual test store.

Live Gmail writes require the explicit confirmation path already implemented by
the runner or `--live` for the automatic worker. Gmail sending is still
unavailable.

## Config Notes

- Keep durable Gmail auth in environment variables:
  `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`.
- Keep ClickHouse credentials in environment variables or secret files.
- Project-specific rule and template paths live in
  `[knowledge.project_rules_paths]` and `[knowledge.project_templates_dirs]`.
- Configure Feishu notifications with:

```toml
[notify]
mode = "feishu"
feishu_webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/..."
```

Default notification mode is file handoff under
`var/player_support_agent/handoffs`.

## Validation

After edits, run:

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

For broad repo changes, run `./.venv/bin/python -m pytest tests/unit`. If proxy
server tests fail inside the sandbox with a local port bind error, rerun with
the required permission.

## Troubleshooting

- If active questions return fixed JSON or command output, check that the entry
  point calls `SupportAgentRunner` instead of routing by keywords.
- If automatic messages are repeatedly retried, inspect
  `var/player_support_agent/processed_messages.json` and confirm the model
  called `save_case_state` for each selected `message_id`.
- If a message routes to the wrong project, inspect Gmail `label_names`,
  `project_label`, and `[gmail].project_label_names`.
- If ClickHouse fails, validate the SQL first and check that it includes an
  allowed table, player filter, time range, and `LIMIT`.
- If Gmail labels fail, call the existing-label tool and confirm the requested
  label is configured and already exists in Gmail.
- If Feishu does not receive failure handoffs, confirm `[notify].mode`,
  `feishu_webhook_url`, and network access.
