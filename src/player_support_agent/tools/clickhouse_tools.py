"""ClickHouse SQL validation and query tools."""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from forge.errors import ToolResolutionError

from ..paths import resolve_project_path
from .config import ClickHouseConfig, ClickHouseEvidenceRecipeConfig


FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|rename|grant|revoke|"
    r"attach|detach|optimize|kill|system|set)\b",
    re.IGNORECASE,
)
TABLE_RE = re.compile(r"\b(?:from|join)\s+([`\"]?[a-zA-Z0-9_.]+[`\"]?)", re.I)
LIMIT_RE = re.compile(r"\blimit\s+(\d+)\b", re.I)


def _strip_identifier(identifier: str) -> str:
    return identifier.strip("`\"")


def _parse_dt(value: str) -> datetime:
    cleaned = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.rstrip("\x00")
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean_value(item) for key, item in value.items()}
    return value


def _compact_row(row: dict[str, Any], max_columns: int = 12) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in list(row.items())[:max_columns]:
        if isinstance(value, str) and len(value) > 160:
            compact[key] = value[:157] + "..."
        else:
            compact[key] = value
    return compact


def _sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class ClickHouseTools:
    """Safe ClickHouse HTTP tools."""

    def __init__(
        self,
        config: ClickHouseConfig,
        *,
        remove_ads_investigation_path: str | None = None,
        coin_frenzy_investigation_path: str | None = None,
    ) -> None:
        self.config = config
        self.remove_ads_investigation_path = (
            remove_ads_investigation_path or "knowledge/remove_ads_investigation.toml"
        )
        self.coin_frenzy_investigation_path = (
            coin_frenzy_investigation_path or "knowledge/coin_frenzy_investigation.toml"
        )

    def _auth(self) -> tuple[str, str] | None:
        username = self.config.resolve_username()
        if username is None:
            return None
        password = self.config.resolve_password()
        return (username, password or "")

    def _log_query_skipped_for_case(self, case_type: str) -> bool:
        return case_type in self.config.skip_log_query_case_types

    def _skip_log_query_reason(self, case_type: str) -> str:
        return (
            f"ClickHouse log queries are disabled for case_type={case_type!r} "
            "per configuration. The model must classify case_type in "
            "extract_feedback_claim; tools do not auto-detect complaint types."
        )

    def _skip_log_query_next_steps(self, case_type: str) -> list[str]:
        return [
            f"Do not query ClickHouse for case_type={case_type!r}.",
            "Call get_relevant_support_rules and follow the matched rule's "
            "instructions for reply template and workflow.",
            "Continue with assess_claim_credibility and decide_support_action "
            "using matched rule_action.",
            "Finish with review_reply_draft, draft or handoff, and save_case_state.",
        ]

    def _ads_after_purchase_catalog_next_steps(self) -> list[str]:
        return [
            "Call get_remove_ads_investigation_playbook for the project workflow.",
            "When matched rules have requires_logs=true, call get_clickhouse_schema "
            "and query_clickhouse with model-generated SELECT (purchase records first, "
            "then AdShow_Inter).",
            "Call assess_remove_ads_log_evidence with structured findings before drafting.",
            "Do not claim purchase or ad behavior was verified without query results.",
            "Continue to assess_claim_credibility and decide_support_action.",
        ]

    def _coin_frenzy_catalog_next_steps(self) -> list[str]:
        return [
            "Call get_coin_frenzy_investigation_playbook for the project workflow.",
            "When pass_purchase_misunderstanding or coin_frenzy rule matches (e.g. starlight pass), "
            "call get_clickhouse_schema and query_clickhouse with model-generated SELECT for purchase records.",
            "Call assess_coin_frenzy_log_evidence with structured findings before drafting.",
            "Do not claim pass purchase was verified without query results.",
            "Continue to assess_claim_credibility and decide_support_action.",
        ]

    def _platform_table_routing(self, project: str | None) -> dict[str, Any] | None:
        if not project:
            return None
        routing = self.config.project_platform_tables.get(project)
        if not routing:
            return None
        return {
            platform: {
                "tables": tables,
                "summary": (
                    f"Use table {tables[0]!r} for {platform} players."
                    if len(tables) == 1
                    else f"Use one of {tables!r} for {platform} players."
                ),
            }
            for platform, tables in sorted(routing.items())
            if tables
        }

    def _recommended_table_for_project(
        self,
        project: str,
        allowed_tables: list[str],
    ) -> str | None:
        routing = self.config.project_platform_tables.get(project)
        if routing:
            for tables in routing.values():
                for table in tables:
                    if table in allowed_tables:
                        return table
        return allowed_tables[0] if allowed_tables else None

    def _load_remove_ads_investigation_playbook(self) -> dict[str, Any]:
        path = resolve_project_path(self.remove_ads_investigation_path)
        if not path.exists():
            return {}
        with path.open("rb") as handle:
            return tomllib.load(handle)

    def get_remove_ads_investigation_playbook(
        self,
        project: str,
        case_type: str = "ads_after_purchase",
    ) -> dict[str, Any]:
        """Return read-only remove-ads log investigation guidance for a project."""

        if case_type != "ads_after_purchase":
            return {
                "available": False,
                "project": project,
                "case_type": case_type,
                "reason": (
                    "Remove-ads investigation playbook applies only to "
                    "case_type=ads_after_purchase."
                ),
            }

        allowed_tables = sorted(
            self._allowed_tables_for_case(case_type, project=project)
        )
        if not allowed_tables:
            return {
                "available": False,
                "project": project,
                "case_type": case_type,
                "reason": f"No ClickHouse table mapping for project {project!r}.",
                "next_steps": [
                    "Do not query ClickHouse for this project.",
                    "Ask for order evidence or hand off to human support.",
                ],
            }

        clickhouse_table = self._recommended_table_for_project(project, allowed_tables)
        table_config = (
            self.config.allowed_schema.get(clickhouse_table) if clickhouse_table else None
        )
        platform_routing = self._platform_table_routing(project)
        playbook = self._load_remove_ads_investigation_playbook()
        next_steps = [
            "Call get_clickhouse_schema(case_type=ads_after_purchase, project=...).",
            "Run purchase-record query_clickhouse, then interstitial query_clickhouse.",
            "Call assess_remove_ads_log_evidence before drafting.",
        ]
        if platform_routing:
            next_steps.insert(
                1,
                "Infer platform from email metadata (platform:iOS/Android) and use "
                "platform_table_routing to pick the correct table.",
            )
        return {
            "available": True,
            "project": project,
            "case_type": case_type,
            "playbook_path": str(resolve_project_path(self.remove_ads_investigation_path)),
            "clickhouse_table": clickhouse_table,
            "allowed_tables": allowed_tables,
            "platform_table_routing": platform_routing,
            "schema_columns": table_config.columns if table_config else [],
            "time_column": table_config.time_column if table_config else "log_time",
            "player_id_columns": (
                table_config.player_id_columns if table_config else ["user_id"]
            ),
            "remove_ads_policy": playbook.get("remove_ads_policy", "").strip(),
            "investigation_steps": playbook.get("investigation_steps", []),
            "purchase_query_hints": playbook.get("purchase_query_hints", {}),
            "interstitial_query_hints": playbook.get("interstitial_query_hints", {}),
            "example_sql": playbook.get("example_sql", []),
            "outcome_branches": playbook.get("outcome_branches", []),
            "next_steps": next_steps,
        }

    def _load_coin_frenzy_investigation_playbook(self) -> dict[str, Any]:
        path = resolve_project_path(self.coin_frenzy_investigation_path)
        if not path.exists():
            return {}
        with path.open("rb") as handle:
            return tomllib.load(handle)

    def get_coin_frenzy_investigation_playbook(
        self,
        project: str,
        case_type: str = "pass_purchase_misunderstanding",
    ) -> dict[str, Any]:
        """Return read-only pass / Coin Frenzy / starlight pass purchase investigation guidance for a project.
        For complaints like "bought starlight pass but did not receive it".
        """

        allowed_case_types = {
            "pass_purchase_misunderstanding",
            "payment",
        }
        if case_type not in allowed_case_types:
            return {
                "available": False,
                "project": project,
                "case_type": case_type,
                "reason": (
                    "Coin Frenzy investigation playbook applies to pass_purchase_misunderstanding "
                    "or payment cases with activity-reward confusion."
                ),
                "next_steps": [
                    "Do not use Coin Frenzy investigation for this case_type.",
                    "If get_relevant_support_rules matched a rule with requires_logs=false, "
                    "skip ClickHouse and continue to assess_claim_credibility and "
                    "decide_support_action.",
                ],
            }

        allowed_tables = sorted(
            self._allowed_tables_for_case(case_type, project=project)
        )
        if not allowed_tables:
            return {
                "available": False,
                "project": project,
                "case_type": case_type,
                "reason": f"No ClickHouse table mapping for project {project!r}.",
                "next_steps": [
                    "Do not query ClickHouse for this project.",
                    "Ask for order evidence or hand off to human support.",
                ],
            }

        clickhouse_table = self._recommended_table_for_project(project, allowed_tables)
        table_config = (
            self.config.allowed_schema.get(clickhouse_table) if clickhouse_table else None
        )
        playbook = self._load_coin_frenzy_investigation_playbook()
        return {
            "available": True,
            "project": project,
            "case_type": case_type,
            "playbook_path": str(resolve_project_path(self.coin_frenzy_investigation_path)),
            "clickhouse_table": clickhouse_table,
            "allowed_tables": allowed_tables,
            "schema_columns": table_config.columns if table_config else [],
            "time_column": table_config.time_column if table_config else "log_time",
            "player_id_columns": (
                table_config.player_id_columns if table_config else ["user_id"]
            ),
            "coin_frenzy_policy": playbook.get("coin_frenzy_policy", "").strip(),
            "investigation_steps": playbook.get("investigation_steps", []),
            "purchase_query_hints": playbook.get("purchase_query_hints", {}),
            "example_sql": playbook.get("example_sql", []),
            "outcome_branches": playbook.get("outcome_branches", []),
            "next_steps": [
                "Call get_clickhouse_schema and query_clickhouse for purchase records.",
                "Call assess_coin_frenzy_log_evidence before drafting.",
                "Use coin_frenzy_task_reward_explanation when product_id is coin.frenzy-like.",
            ],
        }

    def _allowed_tables_for_case(
        self,
        case_type: str,
        project: str | None = None,
    ) -> set[str]:
        if project:
            project_tables = self.config.project_case_type_tables.get(project)
            if project_tables:
                configured = project_tables.get(case_type) or project_tables.get("*")
                if configured:
                    return set(configured)
            return set()
        if self.config.require_project_for_queries:
            return set()
        configured = self.config.case_type_tables.get(case_type)
        if configured:
            return set(configured)
        return set(self.config.allowed_schema.keys())

    def get_clickhouse_schema(
        self,
        case_type: str,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Return the whitelisted schema for a case type."""

        if self._log_query_skipped_for_case(case_type):
            return {
                "project": project,
                "case_type": case_type,
                "allowed_tables": [],
                "schema": {},
                "log_query_skipped": True,
                "reason": self._skip_log_query_reason(case_type),
                "next_steps": self._skip_log_query_next_steps(case_type),
            }

        allowed = self._allowed_tables_for_case(case_type, project=project)
        schema = {
            table: cfg.model_dump()
            for table, cfg in self.config.allowed_schema.items()
            if table in allowed
        }
        platform_routing = self._platform_table_routing(project)
        payload: dict[str, Any] = {
            "project": project,
            "case_type": case_type,
            "allowed_tables": sorted(schema.keys()),
            "schema": schema,
            "rules": {
                "read_only": True,
                "max_rows": self.config.max_rows,
                "max_time_window_hours": self.config.max_time_window_hours,
                "must_filter_player": True,
                "must_filter_time_window": True,
                "sql_time_filter": (
                    "Include literal log_time >= start (for pass: >= email_date - 5 days, no end time condition). Do not use now() - INTERVAL."
                ),
            },
        }
        if platform_routing:
            payload["platform_table_routing"] = platform_routing
            payload["rules"]["platform_selection"] = (
                "When platform_table_routing is present, pick the table from the "
                "player platform in the email (platform:iOS or platform:Android)."
            )
        return payload

    def get_support_evidence_catalog(
        self,
        project: str,
        case_type: str,
    ) -> dict[str, Any]:
        """Return configured evidence kinds for a project and case type."""

        if self._log_query_skipped_for_case(case_type):
            return {
                "project": project,
                "case_type": case_type,
                "available": False,
                "reason": self._skip_log_query_reason(case_type),
                "evidence_kinds": [],
                "skip_clickhouse_fallback": True,
                "log_query_skipped": True,
                "next_steps": self._skip_log_query_next_steps(case_type),
            }

        recipes = self._matching_evidence_recipes(project, case_type)
        if not recipes:
            if case_type == "ads_after_purchase":
                return {
                    "project": project,
                    "case_type": case_type,
                    "available": False,
                    "reason": (
                        "No configured evidence recipes; use remove-ads investigation "
                        "playbook and model-generated SQL."
                    ),
                    "evidence_kinds": [],
                    "skip_clickhouse_fallback": False,
                    "next_steps": self._ads_after_purchase_catalog_next_steps(),
                }
            if case_type == "pass_purchase_misunderstanding":
                return {
                    "project": project,
                    "case_type": case_type,
                    "available": False,
                    "reason": (
                        "No configured evidence recipes; use Coin Frenzy investigation "
                        "playbook when coin_frenzy_activity_log_investigation matches."
                    ),
                    "evidence_kinds": [],
                    "skip_clickhouse_fallback": False,
                    "next_steps": self._coin_frenzy_catalog_next_steps(),
                }
            return {
                "project": project,
                "case_type": case_type,
                "available": False,
                "reason": "No configured evidence recipes for this project and case type.",
                "evidence_kinds": [],
                "skip_clickhouse_fallback": True,
                "next_steps": [
                    "Do not call get_clickhouse_schema, validate_clickhouse_sql, "
                    "or query_clickhouse unless a matched support rule has "
                    "requires_logs=true.",
                    "Call assess_claim_credibility with evidence_status=unavailable "
                    "or insufficient, then decide_support_action using the matched "
                    "support rule.",
                    "Do not repeat get_relevant_support_rules or re-read the thread; "
                    "continue to decide_support_action and save_case_state.",
                ],
            }
        return {
            "project": project,
            "case_type": case_type,
            "available": True,
            "reason": None,
            "evidence_kinds": [
                {
                    "id": recipe.id,
                    "table": recipe.table,
                    "select_columns": recipe.select_columns,
                    "event_names": recipe.event_names,
                    "supported_when": recipe.supported_when,
                }
                for recipe in recipes
            ],
        }

    async def query_support_evidence(
        self,
        project: str,
        case_type: str,
        player_id: str,
        time_window_start: str,
        time_window_end: str,
        evidence_kind: str,
    ) -> dict[str, Any]:
        """Run a configured evidence recipe and return a compact judgment."""

        if self._log_query_skipped_for_case(case_type):
            return {
                "project": project,
                "case_type": case_type,
                "evidence_kind": evidence_kind,
                "available": False,
                "status": "skipped",
                "log_query_skipped": True,
                "reason": self._skip_log_query_reason(case_type),
                "next_steps": self._skip_log_query_next_steps(case_type),
            }

        allowed_tables = self._allowed_tables_for_case(case_type, project=project)
        if not allowed_tables:
            return {
                "project": project,
                "case_type": case_type,
                "evidence_kind": evidence_kind,
                "available": False,
                "status": "unavailable",
                "reason": f"No allowed ClickHouse tables for {case_type!r}",
            }

        recipe = self._find_evidence_recipe(project, case_type, evidence_kind)
        if recipe is None:
            return {
                "project": project,
                "case_type": case_type,
                "evidence_kind": evidence_kind,
                "available": False,
                "status": "unavailable",
                "reason": "No configured evidence recipe matched this request.",
            }
        if recipe.table not in allowed_tables:
            return {
                "project": project,
                "case_type": case_type,
                "evidence_kind": evidence_kind,
                "available": False,
                "status": "unavailable",
                "reason": f"Evidence recipe table {recipe.table!r} is not allowed for this project/case type.",
            }

        try:
            sql = self._build_recipe_sql(
                recipe,
                player_id=player_id,
                time_window_start=time_window_start,
                time_window_end=time_window_end,
            )
        except ToolResolutionError as exc:
            return {
                "project": project,
                "case_type": case_type,
                "evidence_kind": evidence_kind,
                "available": False,
                "status": "unavailable",
                "reason": str(exc),
            }

        result = await self.query_clickhouse(
            sql=sql,
            player_id=player_id,
            time_window_start=time_window_start,
            time_window_end=time_window_end,
            case_type=case_type,
            project=project,
        )
        row_count = int(result.get("row_count", 0))
        supported = row_count > 0 if recipe.supported_when == "any_row" else row_count == 0
        status = "supported" if supported else "contradicted"
        return {
            "project": project,
            "case_type": case_type,
            "evidence_kind": evidence_kind,
            "available": True,
            "status": status,
            "confidence": 0.85 if supported else 0.45,
            "facts": [
                f"Evidence recipe {recipe.id!r} returned {row_count} row(s).",
            ],
            "missing_data": [],
            "sql_used": result.get("sql", sql),
            "query_result_summary": result.get("summary", {}),
        }

    def validate_clickhouse_sql(
        self,
        sql: str,
        player_id: str,
        time_window_start: str,
        time_window_end: str,
        case_type: str,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Validate a model-generated ClickHouse query before execution."""

        if self._log_query_skipped_for_case(case_type):
            return {
                "ok": False,
                "reason": self._skip_log_query_reason(case_type),
                "sql": sql,
                "project": project,
                "log_query_skipped": True,
                "next_steps": self._skip_log_query_next_steps(case_type),
            }

        try:
            normalized = self._validate_or_raise(
                sql=sql,
                player_id=player_id,
                time_window_start=time_window_start,
                time_window_end=time_window_end,
                case_type=case_type,
                project=project,
            )
        except ToolResolutionError as exc:
            return {
                "ok": False,
                "reason": str(exc),
                "sql": sql,
                "hint": (
                    "Fix the SQL once using get_clickhouse_schema, or skip SQL and "
                    "continue with assess_claim_credibility when logs are optional."
                ),
            }
        return {
            "ok": True,
            "reason": None,
            "sql": normalized,
            "project": project,
            "hint": (
                "SQL passed validation. Call query_clickhouse next with this exact "
                "sql value, or skip query_clickhouse if evidence is already enough."
            ),
        }

    def _matching_evidence_recipes(
        self,
        project: str,
        case_type: str,
    ) -> list[ClickHouseEvidenceRecipeConfig]:
        return [
            recipe
            for recipe in self.config.evidence_recipes
            if (not recipe.projects or project in recipe.projects)
            and (not recipe.case_types or case_type in recipe.case_types)
        ]

    def _find_evidence_recipe(
        self,
        project: str,
        case_type: str,
        evidence_kind: str,
    ) -> ClickHouseEvidenceRecipeConfig | None:
        for recipe in self._matching_evidence_recipes(project, case_type):
            if recipe.id == evidence_kind:
                return recipe
        return None

    def _build_recipe_sql(
        self,
        recipe: ClickHouseEvidenceRecipeConfig,
        *,
        player_id: str,
        time_window_start: str,
        time_window_end: str,
    ) -> str:
        table_config = self.config.allowed_schema.get(recipe.table)
        if table_config is None:
            raise ToolResolutionError(
                f"Evidence recipe table {recipe.table!r} is not in allowed_schema"
            )
        columns = set(table_config.columns)
        unknown_columns = set(recipe.select_columns) - columns
        if unknown_columns:
            raise ToolResolutionError(
                f"Evidence recipe selects non-whitelisted columns: {sorted(unknown_columns)}"
            )

        player_column = recipe.player_column or table_config.player_id_columns[0]
        time_column = recipe.time_column or table_config.time_column
        if player_column not in columns:
            raise ToolResolutionError(
                f"Evidence recipe player column {player_column!r} is not whitelisted"
            )
        if time_column not in columns:
            raise ToolResolutionError(
                f"Evidence recipe time column {time_column!r} is not whitelisted"
            )
        where = [
            f"{player_column} = {_sql_literal(player_id)}",
            f"{time_column} >= {_sql_literal(time_window_start)}",
            f"{time_column} < {_sql_literal(time_window_end)}",
        ]
        if recipe.event_names:
            if "event_name" not in columns:
                raise ToolResolutionError(
                    "Evidence recipe uses event_names but event_name is not whitelisted"
                )
            values = ", ".join(_sql_literal(value) for value in recipe.event_names)
            where.append(f"event_name IN ({values})")
        for column, value in recipe.filters.items():
            if column not in columns:
                raise ToolResolutionError(
                    f"Evidence recipe filter column {column!r} is not whitelisted"
                )
            where.append(f"{column} = {_sql_literal(value)}")
        if recipe.product_id_contains:
            if "product_id" not in columns:
                raise ToolResolutionError(
                    "Evidence recipe uses product_id_contains but product_id is not whitelisted"
                )
            likes = [
                f"product_id LIKE {_sql_literal('%' + value + '%')}"
                for value in recipe.product_id_contains
            ]
            where.append("(" + " OR ".join(likes) + ")")

        limit = min(max(1, recipe.limit), self.config.max_rows)
        return (
            f"SELECT {', '.join(recipe.select_columns)} FROM {recipe.table} "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY {time_column} DESC "
            f"LIMIT {limit}"
        )

    def _validate_or_raise(
        self,
        sql: str,
        player_id: str,
        time_window_start: str,
        time_window_end: str,
        case_type: str,
        project: str | None = None,
    ) -> str:
        if self._log_query_skipped_for_case(case_type):
            raise ToolResolutionError(self._skip_log_query_reason(case_type))

        raw = sql.strip()
        if not raw:
            raise ToolResolutionError("SQL is empty")
        if raw.count(";") > 1 or (";" in raw and not raw.endswith(";")):
            raise ToolResolutionError("Multiple SQL statements are not allowed")
        raw = raw.rstrip(";").strip()
        lowered = raw.lower()
        if not lowered.startswith("select"):
            raise ToolResolutionError("Only SELECT queries are allowed")
        if "/*" in raw or "--" in raw:
            raise ToolResolutionError("SQL comments are not allowed")
        if FORBIDDEN_SQL.search(raw):
            raise ToolResolutionError("SQL contains forbidden write/admin keyword")
        if re.search(r"\bselect\s+\*", raw, re.I):
            raise ToolResolutionError("SELECT * is not allowed")

        allowed_tables = self._allowed_tables_for_case(case_type, project=project)
        if not allowed_tables:
            raise ToolResolutionError(f"No allowed ClickHouse tables for {case_type!r}")

        found_tables = {_strip_identifier(m.group(1)) for m in TABLE_RE.finditer(raw)}
        if not found_tables:
            raise ToolResolutionError("SQL must reference at least one table")
        unknown_tables = found_tables - allowed_tables
        if unknown_tables:
            raise ToolResolutionError(
                f"SQL references non-whitelisted table(s): {sorted(unknown_tables)}"
            )

        limit_match = LIMIT_RE.search(raw)
        if not limit_match:
            raise ToolResolutionError("SQL must include LIMIT")
        limit = int(limit_match.group(1))
        if limit > self.config.max_rows:
            raise ToolResolutionError(
                f"LIMIT {limit} exceeds max_rows {self.config.max_rows}"
            )

        start = _parse_dt(time_window_start)
        end = _parse_dt(time_window_end)
        if end <= start:
            raise ToolResolutionError("time_window_end must be after start")
        hours = (end - start).total_seconds() / 3600
        if hours > self.config.max_time_window_hours:
            raise ToolResolutionError(
                f"Time window {hours:.1f}h exceeds max "
                f"{self.config.max_time_window_hours}h"
            )

        table_configs = [self.config.allowed_schema[t] for t in found_tables]
        player_columns = {
            col for cfg in table_configs for col in cfg.player_id_columns
        }
        time_columns = {cfg.time_column for cfg in table_configs}

        if not any(re.search(rf"\b{re.escape(col)}\b", raw) for col in player_columns):
            raise ToolResolutionError(
                f"SQL must filter by one player column: {sorted(player_columns)}"
            )
        if player_id not in raw:
            raise ToolResolutionError("SQL must include the expected player_id value")
        if not any(re.search(rf"\b{re.escape(col)}\b", raw) for col in time_columns):
            raise ToolResolutionError(
                f"SQL must filter by one time column: {sorted(time_columns)}"
            )
        if time_window_start[:10] not in raw:
            raise ToolResolutionError("SQL must include the requested start time window date")
        if case_type != "pass_purchase_misunderstanding":
            if time_window_end[:10] not in raw:
                raise ToolResolutionError("SQL must include the requested time window end date")
        # For pass_purchase_misunderstanding: only start time >= (feedback-5d), no end time condition required

        return raw

    async def query_clickhouse(
        self,
        sql: str,
        player_id: str,
        time_window_start: str,
        time_window_end: str,
        case_type: str,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Execute a validated ClickHouse query through the HTTP API."""

        validated_sql = self._validate_or_raise(
            sql=sql,
            player_id=player_id,
            time_window_start=time_window_start,
            time_window_end=time_window_end,
            case_type=case_type,
            project=project,
        )
        query = validated_sql
        if " format " not in query.lower():
            query = f"{query} FORMAT JSONEachRow"

        params: dict[str, Any] = {}
        if self.config.database:
            params["database"] = self.config.database

        timeout = httpx.Timeout(
            connect=self.config.connect_timeout_seconds,
            read=self.config.query_timeout_seconds,
            write=self.config.query_timeout_seconds,
            pool=self.config.connect_timeout_seconds,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                self.config.url,
                params=params,
                content=query.encode("utf-8"),
                auth=self._auth(),
            )
            resp.raise_for_status()

        rows = [
            _clean_value(json.loads(line))
            for line in resp.text.splitlines()
            if line.strip()
        ]
        summary = self._summarize_query_rows(
            rows,
            project=project,
            case_type=case_type,
            sql=validated_sql,
        )
        return {
            "sql": validated_sql,
            "row_count": len(rows),
            "summary": summary,
        }

    def _summarize_query_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        project: str | None,
        case_type: str,
        sql: str,
    ) -> dict[str, Any]:
        found_tables = {_strip_identifier(m.group(1)) for m in TABLE_RE.finditer(sql)}
        table_configs = [
            self.config.allowed_schema[table]
            for table in found_tables
            if table in self.config.allowed_schema
        ]
        time_columns = [cfg.time_column for cfg in table_configs]
        player_columns = sorted(
            {col for cfg in table_configs for col in cfg.player_id_columns}
        )
        event_column = next(
            (
                col
                for col in ("event_name", "event", "action", "type", "status")
                if any(col in row for row in rows)
            ),
            None,
        )
        times = [
            str(row[col])
            for row in rows
            for col in time_columns
            if row.get(col) is not None
        ]
        events = [
            str(row.get(event_column))
            for row in rows
            if event_column and row.get(event_column) is not None
        ]
        return {
            "project": project,
            "case_type": case_type,
            "tables": sorted(found_tables),
            "row_count": len(rows),
            "time_columns": time_columns,
            "player_id_columns": player_columns,
            "first_event_time": min(times) if times else None,
            "last_event_time": max(times) if times else None,
            "event_column": event_column,
            "event_counts": dict(Counter(events).most_common(20)),
            "sample_rows": [_compact_row(row) for row in rows[:5]],
            "raw_rows_omitted": True,
        }

    def summarize_behavior_logs(
        self,
        rows: list[dict[str, Any]],
        time_column: str = "event_time",
        event_column: str = "event_name",
    ) -> dict[str, Any]:
        """Summarize raw behavior rows before giving them back to the model."""

        times = [str(row.get(time_column)) for row in rows if row.get(time_column)]
        events = [
            str(row.get(event_column))
            for row in rows
            if row.get(event_column) is not None
        ]
        sample_rows = rows[:10]
        return {
            "row_count": len(rows),
            "first_event_time": min(times) if times else None,
            "last_event_time": max(times) if times else None,
            "event_counts": dict(Counter(events).most_common(20)),
            "sample_rows": sample_rows,
        }
