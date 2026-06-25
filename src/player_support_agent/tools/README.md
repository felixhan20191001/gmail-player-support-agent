# Player Support Agent Tools

This folder contains business tools for a Forge-based player-support agent.
They live outside `src/forge` on purpose: Forge provides the reliable
tool-calling loop, while these modules handle Gmail, ClickHouse, decisions,
human handoff, and local audit state.

## Tools

- Gmail: `list_new_feedback_emails`, `list_unread_inbox_emails`,
  `list_unread_project_emails`, `read_email_thread`,
  `get_existing_gmail_labels`, `apply_existing_gmail_labels`,
  `create_gmail_draft`
- ClickHouse: `get_clickhouse_schema`, `validate_clickhouse_sql`,
  `query_clickhouse`, `summarize_behavior_logs`
- Decisions: `extract_feedback_claim`, `resolve_player_identity`,
  `assess_claim_credibility`, `decide_support_action`
- Human support: `create_human_handoff_summary`, `notify_human_support`
- State/audit: `get_case_state`, `save_case_state`, `write_audit_log`

## Safety Defaults

- Gmail labels are never created by tools.
- Gmail sends are not implemented; the only outbound mail action is draft
  creation.
- `list_unread_inbox_emails` is read-only and returns metadata/snippets, not
  full message bodies.
- Label mutation only adds existing labels and requires `gmail.allowed_label_names`;
  label removal is not exposed to the model.
- ClickHouse queries are validated before execution and revalidated inside
  `query_clickhouse`.
- ClickHouse is limited to `SELECT`, whitelisted tables, player scoping, time
  scoping, and `LIMIT`.
- ClickHouse query results return compact summaries instead of full raw rows.
- Notification modes include `file`, `webhook`, `feishu`, `smtp`, and `none`.
  Failed automatic runs use the same human-support notification path.

## Config

Copy `config.example.toml` and fill in your Gmail, ClickHouse, and notification
settings. Prefer environment variables for secrets:

```bash
export GMAIL_ACCESS_TOKEN="..."
export CLICKHOUSE_PASSWORD="..."
```

Then load it:

```python
from examples.player_support_agent.tools import build_tool_defs, load_config

config = load_config("examples/player_support_agent/tools/config.example.toml")
tools = build_tool_defs(config)
```
