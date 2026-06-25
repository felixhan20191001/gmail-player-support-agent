# Repository Instructions

This repository is a standalone multi-project Gmail player support agent built
on the `forge-guardrails` Python package (`import forge`). Business code lives
under `src/player_support_agent`. These instructions are binding for future
coding agents that modify this workspace.

## Player Support Agent Architecture

For changes under `src/player_support_agent` and `tests/unit/`:

- User-initiated requests must enter the local model through
  `SupportAgentRunner`, which uses Forge `WorkflowRunner`.
- CLI and Web entrypoints must not route business behavior with keywords,
  regular expressions, or if/else chains that directly decide Gmail,
  ClickHouse, SQL, labels, drafts, or handoff actions.
- The automatic worker may directly call Gmail only to discover candidate
  message/thread IDs, deduplicate them, build a natural-language task, and call
  the shared agent runner.
- Gmail parent labels are treated as project names. The worker may attach
  project label hints to the natural-language task, but project-specific
  business decisions still belong to the model and tools.
- Scheduler/worker code must not classify emails, generate SQL, judge
  credibility, choose business labels, create drafts, or decide handoff policy.
  Those decisions belong to the model through registered Forge tools.
- Do not replace the WorkflowRunner loop with a single proxy request or a
  manually chained Gmail -> SQL -> draft script.

## Gmail Safety

- Gmail tools must never send email.
- Gmail tools must never delete, trash, archive, or bulk-destructively modify
  messages.
- Gmail tools must never create labels. They may only read existing labels and
  apply configured existing labels after validation.
- If the model requests an unknown or unconfigured label, the tool layer must
  reject the request.
- Current production behavior may create Gmail drafts only. Live writes require
  the explicit confirmation phrase used by the runner.
- Never log or print OAuth tokens, refresh tokens, client secrets, full Gmail API
  responses, or full private email bodies.

## ClickHouse Safety

- SQL execution must go through the validator before reaching ClickHouse.
- SQL must be read-only `SELECT`.
- Reject `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, `CREATE`,
  or other write/schema mutation statements.
- SQL must use allowed tables/columns, a player identifier when querying player
  logs, a bounded time range, and `LIMIT`.
- Project-specific ClickHouse routing belongs in configuration/tool validation,
  not in entrypoint keyword logic.
- If a model supplies a project and that project has no ClickHouse table
  mapping, fail closed instead of falling back to global or another project's
  tables.
- In multi-project deployments, prefer `require_project_for_queries = true` so
  ClickHouse logs cannot be queried before a project is identified.
- ClickHouse tool results returned to the model should be compact summaries, not
  large raw result sets.

## State And Handoff

- Candidate selection for every mail-processing entrypoint (manual Web run,
  sender-filter run, automation worker, and future discovery-to-run features)
  must go through `ProcessedMessageStore.select_candidates_for_run` after
  Gmail UNREAD state is fetched for the discovered candidates.
- Gmail UNREAD is the source of truth for whether a message still needs work:
  if Gmail still marks a candidate UNREAD, select it for processing even when
  the local store already has a terminal or `failed` outcome.
- Only Gmail READ candidates with an existing terminal or `failed` store record
  may be skipped as already handled. The sole exception is `processing`, which
  remains skipped to avoid overlapping runs on the same message.
- Do not add entrypoint-specific dedupe rules that skip UNREAD mail because of
  local terminal state. New features must reuse the shared selection helper and
  UNREAD lookup rather than reimplementing store filters.
- Non-player-feedback discovery is the exception: messages without configured
  project Gmail labels and without a configured project name in the subject may
  be filtered in `partition_player_feedback_candidates` before model selection.
  Known non-game sender patterns from config may also be ignored. Persist these
  as `skip_category=non_project` so they are not reprocessed while still UNREAD.
- Automatic processing must persist per-message state in the processed-message
  store.
- Successful automatic processing must be based on model-provided
  `save_case_state` outcomes such as `draft_created`, `human_review`, `failed`,
  or `skipped`.
- If the model does not save state for a selected automatic message, mark that
  message as `failed` so it can be retried or investigated.
- Failed automatic cases should produce a human handoff notification through the
  configured notification mode.
- Active terminal/Web chat runs should also record run status, but only store
  short input/answer previews and safe metadata.

## Model Runtime Policy

- Business behavior for the local model belongs in `prompts.py`, support rules,
  reply templates, tool descriptions, and tool schemas.
- Do not rely on `AGENTS.md` or Codex skills as runtime policy for the local
  model.
- If a behavior must affect model decisions, update the model-facing prompt,
  rules, templates, or tools and add focused tests.
- For project-specific Gmail labels, prefer config/tool support such as
  `label_suffix_by_case_type` plus existing-label validation over hard-coded
  NumberCrush-only label names.

## Testing

After changing the player support agent, run focused tests:

```bash
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

Also run:

```bash
.venv/bin/python -m compileall src/player_support_agent
```

Forge framework changes belong in the upstream `forge-guardrails` project, not
in this repository. Bump the `forge-guardrails` dependency here when a newer
framework release is required.
