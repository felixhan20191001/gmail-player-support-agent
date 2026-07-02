"""Lightweight support-rule and reply-template tools."""

from __future__ import annotations

from pathlib import Path
import re
import tomllib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from forge.errors import ToolResolutionError

from ..paths import resolve_project_path
from .config import KnowledgeConfig
from .feedback_text import has_substantive_feedback_text


SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SAFE_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TEMPLATE_LANGUAGE_ALIASES = {
    "english": "en",
    "en-us": "en",
    "en-gb": "en",
    "dutch": "nl",
    "nederlands": "nl",
    "nl-nl": "nl",
    "chinese": "zh-CN",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
}


def _normalize_template_language(language: str) -> str:
    normalized = language.strip().replace("_", "-")
    alias = _TEMPLATE_LANGUAGE_ALIASES.get(normalized.casefold())
    return alias or normalized


_POLICY_ONLY_RULE_IDS = frozenset({"never_ask_server_or_character_name"})

_EMAIL_TEXT_REMINDER = (
    "email_text must be copied verbatim from read_email_thread player feedback; "
    "do not paraphrase, substitute another ticket, or invent wording."
)


def _is_policy_only_rule(rule: SupportRule) -> bool:
    if rule.id in _POLICY_ONLY_RULE_IDS:
        return True
    return (
        rule.always_include
        and not rule.reply_template
        and rule.action == "draft_reply"
    )


def _recommended_actionable_rule(matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in matches:
        if item.get("policy_only"):
            continue
        if item.get("reply_template") or item.get("action") == "apply_label_only":
            return item
    return matches[0] if matches else None


class LegacyReplyTemplate(BaseModel):
    """Imported human-authored reply template from legacy support library."""

    model_config = ConfigDict(extra="forbid")

    id: str
    summary: str = ""
    projects: list[str] = Field(default_factory=list)
    case_types: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    body_path: str
    priority: int = 45
    source_file: str = ""


class SupportRule(BaseModel):
    """One business rule from the support knowledge base."""

    model_config = ConfigDict(extra="forbid")

    id: str
    projects: list[str] = Field(default_factory=list)
    case_types: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    priority: int = 0
    summary: str
    requires_logs: bool = False
    required_evidence: list[str] = Field(default_factory=list)
    condition: str | None = None
    action: str = "draft_reply"
    human_review: bool = False
    reply_template: str | None = None
    always_include: bool = False
    require_trigger_match: bool = False
    instructions: list[str] = Field(default_factory=list)
    log_query: dict[str, Any] | None = None


class RuleTools:
    """Read deterministic support rules and reply templates for the model."""

    def __init__(
        self,
        config: KnowledgeConfig,
        *,
        default_language: str = "zh-CN",
        project_label_names: list[str] | None = None,
        clickhouse_project_case_type_tables: dict[str, dict[str, list[str]]] | None = None,
        label_suffix_by_case_type: dict[str, list[str]] | None = None,
    ) -> None:
        self.config = config
        self.default_language = default_language
        self.project_label_names = project_label_names or []
        self.clickhouse_project_case_type_tables = clickhouse_project_case_type_tables or {}
        self.label_suffix_by_case_type = label_suffix_by_case_type or {}

    def _rules_path(self, project: str | None = None) -> Path:
        if project and project in self.config.project_rules_paths:
            return Path(self.config.project_rules_paths[project]).expanduser()
        return Path(self.config.rules_path).expanduser()

    def _rules_paths(self, project: str | None = None) -> list[Path]:
        default_path = Path(self.config.rules_path).expanduser()
        paths = [default_path]
        if project and project in self.config.project_rules_paths:
            project_path = Path(self.config.project_rules_paths[project]).expanduser()
            if project_path != default_path:
                paths.append(project_path)
        return paths

    def _templates_dir(self, project: str | None = None) -> Path:
        if project and project in self.config.project_templates_dirs:
            return Path(self.config.project_templates_dirs[project]).expanduser()
        return Path(self.config.templates_dir).expanduser()

    def _profile_path(self, project: str) -> Path | None:
        if project in self.config.project_profiles_paths:
            return Path(self.config.project_profiles_paths[project]).expanduser()
        if not self.config.project_profiles_dir:
            return None
        safe_name = project.replace("/", "_")
        return Path(self.config.project_profiles_dir).expanduser() / f"{safe_name}.toml"

    def _legacy_templates_path(self) -> Path:
        return resolve_project_path(self.config.legacy_templates_path)

    def _load_legacy_templates(self) -> list[LegacyReplyTemplate]:
        path = self._legacy_templates_path()
        if not path.exists():
            return []
        with path.open("rb") as f:
            data = tomllib.load(f)
        return [
            LegacyReplyTemplate.model_validate(item)
            for item in data.get("templates", [])
        ]

    def _match_legacy_reply_templates(
        self,
        *,
        case_type: str,
        email_text: str,
        project: str | None,
        include_case_defaults: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        normalized_text = email_text.casefold()
        matches: list[dict[str, Any]] = []

        for template in self._load_legacy_templates():
            project_match = not template.projects or (
                project is not None and project in template.projects
            )
            if template.projects and not project_match:
                continue
            case_match = not template.case_types or case_type in template.case_types
            if template.case_types and not case_match:
                continue

            trigger_hits = [
                trigger
                for trigger in template.triggers
                if trigger.casefold() in normalized_text
            ]
            should_include = bool(trigger_hits)
            if (
                include_case_defaults
                and not trigger_hits
                and template.projects
                and project_match
            ):
                should_include = True
            if (
                include_case_defaults
                and not trigger_hits
                and not template.projects
                and template.case_types
                and case_type in template.case_types
            ):
                should_include = True
            if not should_include:
                continue

            score = template.priority
            if case_type in template.case_types:
                score += 20
            if template.projects and project_match:
                score += 25
            score += 10 * len(trigger_hits)
            matches.append(
                {
                    "id": template.id,
                    "score": score,
                    "priority": template.priority,
                    "projects": template.projects,
                    "trigger_hits": trigger_hits,
                    "case_types": template.case_types,
                    "summary": template.summary or template.id,
                    "requires_logs": False,
                    "required_evidence": [],
                    "condition": None,
                    "action": "draft_reply",
                    "human_review": False,
                    "reply_template": template.id,
                    "instructions": [
                        "Legacy human-authored reply template.",
                        f"Source: {template.source_file}",
                        "Adapt wording to the player's language and specific case details.",
                    ],
                    "log_query": None,
                    "source": "legacy_template",
                    "body_path": template.body_path,
                }
            )

        matches.sort(key=lambda item: item["score"], reverse=True)
        return matches[:limit]

    def search_legacy_reply_templates(
        self,
        case_type: str,
        email_text: str,
        project: str | None = None,
        max_templates: int | None = None,
        include_case_defaults: bool = True,
    ) -> dict[str, Any]:
        """Return legacy human reply templates matching project, case type, and wording."""

        limit = max(1, min(max_templates or 8, 20))
        matched = self._match_legacy_reply_templates(
            case_type=case_type,
            email_text=email_text,
            project=project,
            include_case_defaults=include_case_defaults,
            limit=limit,
        )
        return {
            "project": project,
            "case_type": case_type,
            "catalog_path": str(self._legacy_templates_path()),
            "template_count": len(self._load_legacy_templates()),
            "matched_templates": matched,
        }

    def _load_rules(self, project: str | None = None) -> list[SupportRule]:
        rules: list[SupportRule] = []
        for path in self._rules_paths(project):
            if not path.exists():
                continue
            with path.open("rb") as f:
                data = tomllib.load(f)
            rules.extend(SupportRule.model_validate(item) for item in data.get("rules", []))
        return rules

    def get_relevant_support_rules(
        self,
        case_type: str,
        email_text: str,
        project: str | None = None,
        max_rules: int | None = None,
        include_case_defaults: bool = True,
    ) -> dict[str, Any]:
        """Return support rules matching the case type and email wording."""

        limit = max(1, min(max_rules or self.config.max_rules, 20))
        normalized_text = email_text.casefold()
        no_content_rejected = (
            case_type == "no_content"
            and has_substantive_feedback_text(email_text)
        )
        matches: list[dict[str, Any]] = []

        for rule in self._load_rules(project):
            if no_content_rejected and rule.id == "empty_feedback_apply_no_content_label":
                continue
            project_match = not rule.projects or project in rule.projects
            if not project_match:
                continue
            case_match = not rule.case_types or case_type in rule.case_types
            if not case_match:
                continue

            trigger_hits = [
                trigger
                for trigger in rule.triggers
                if trigger.casefold() in normalized_text
            ]
            should_include = bool(trigger_hits) or rule.always_include
            if (
                include_case_defaults
                and not trigger_hits
                and not rule.always_include
                and not rule.require_trigger_match
                and rule.case_types
                and case_type in rule.case_types
            ):
                should_include = True
            if not should_include:
                continue

            score = rule.priority + (20 if case_type in rule.case_types else 0)
            score += 10 * len(trigger_hits)
            policy_only = _is_policy_only_rule(rule)
            if policy_only:
                score -= 60
            matches.append(
                {
                    "id": rule.id,
                    "score": score,
                    "priority": rule.priority,
                    "projects": rule.projects,
                    "trigger_hits": trigger_hits,
                    "case_types": rule.case_types,
                    "summary": rule.summary,
                    "requires_logs": rule.requires_logs,
                    "required_evidence": rule.required_evidence,
                    "condition": rule.condition,
                    "action": rule.action,
                    "human_review": rule.human_review,
                    "reply_template": rule.reply_template,
                    "instructions": rule.instructions,
                    "log_query": rule.log_query,
                    "policy_only": policy_only,
                    "weak_match": not trigger_hits and not rule.always_include,
                }
            )

        matches.sort(key=lambda item: item["score"], reverse=True)
        limited_matches = matches[:limit]
        limited_ids = {item["id"] for item in limited_matches}
        for item in matches:
            if item.get("policy_only") and item["id"] not in limited_ids:
                limited_matches.append(item)
                limited_ids.add(item["id"])
        has_strong_match = any(
            item["trigger_hits"] for item in limited_matches if not item.get("policy_only")
        )
        recommended = _recommended_actionable_rule(limited_matches)
        guidance: str | None = None
        if no_content_rejected:
            guidance = (
                "case_type=no_content was rejected because email_text contains "
                "substantive player feedback after the form marker. Do not apply "
                "the 无内容 label. Re-run extract_feedback_claim once with a "
                "specific non-no_content case type that matches the actual complaint, "
                "then continue with the normal draft or handoff workflow."
            )
        elif (
            recommended
            and has_strong_match
            and not recommended.get("requires_logs")
            and recommended.get("reply_template")
        ):
            guidance = (
                f"Top rule {recommended['id']} has requires_logs=false. "
                "Skip get_support_evidence_catalog ClickHouse paths, "
                "get_coin_frenzy_investigation_playbook, get_clickhouse_schema, "
                "and query_clickhouse. "
                "Next: call resolve_player_identity (pass the player_id from extract_feedback_claim or the email body; only player_id matters), "
                "then assess_claim_credibility (use evidence_status=sufficient if rule matched), "
                "decide_support_action (pass the matched rule's rule_action and id), "
                "review_reply_draft, create_gmail_draft if appropriate, then save_case_state immediately. "
                "Do not re-read the thread, change case_type, call get_reply_template again, or call "
                "get_relevant_support_rules again with different email_text."
            )
        elif not has_strong_match and limited_matches:
            guidance = (
                "No rule trigger matched the email text. Confirm the top actionable "
                "rule fits the player's complaint; if none fit, prefer "
                "vague_issue_details_request or handoff. "
                "Still call resolve_player_identity next (using player_id from extract), then "
                "proceed to assess_claim_credibility and decide_support_action without "
                "re-calling get_relevant_support_rules."
            )
        legacy_limit = max(3, min(limit, 8))
        legacy_matches = self._match_legacy_reply_templates(
            case_type=case_type,
            email_text=email_text,
            project=project,
            include_case_defaults=include_case_defaults,
            limit=legacy_limit,
        )
        return {
            "project": project,
            "case_type": case_type,
            "rules_path": str(self._rules_path(project)),
            "rules_paths": [str(path) for path in self._rules_paths(project)],
            "matched_rules": limited_matches,
            "has_strong_match": has_strong_match,
            "recommended_rule_id": recommended["id"] if recommended else None,
            "no_content_rejected": no_content_rejected,
            "email_text_reminder": _EMAIL_TEXT_REMINDER,
            "guidance": guidance,
            "legacy_templates_path": str(self._legacy_templates_path()),
            "matched_legacy_templates": legacy_matches,
        }

    def get_support_knowledge_summary(self, project: str | None = None) -> dict[str, Any]:
        """Return a safe summary of configured rules and templates."""

        rules = self._load_rules(project)
        template_dir = self._templates_dir(project)
        templates = sorted(template_dir.glob("*/*.md")) if template_dir.exists() else []
        legacy_templates = self._load_legacy_templates()
        legacy_for_project = [
            item
            for item in legacy_templates
            if not item.projects or (project and project in item.projects)
        ]
        return {
            "project": project,
            "rules_path": str(self._rules_path(project)),
            "rules_paths": [str(path) for path in self._rules_paths(project)],
            "rule_count": len(rules),
            "rule_ids": [rule.id for rule in rules],
            "templates_dir": str(template_dir),
            "template_count": len(templates),
            "template_languages": sorted({path.parent.name for path in templates}),
            "legacy_templates_path": str(self._legacy_templates_path()),
            "legacy_template_count": len(legacy_templates),
            "legacy_template_count_for_project": len(legacy_for_project),
            "configured_projects": sorted(
                set(self.config.project_rules_paths)
                | set(self.config.project_templates_dirs)
            ),
        }

    def get_project_support_profile(self, project: str) -> dict[str, Any]:
        """Return a configured project profile or a safe derived summary."""

        profile_path = self._profile_path(project)
        safe_summary = self._project_coverage(project)
        if profile_path and profile_path.exists():
            with profile_path.open("rb") as f:
                profile = tomllib.load(f)
            return {
                "project": project,
                "profile_found": True,
                "profile_path": str(profile_path),
                "profile": profile,
                "safe_summary": safe_summary,
            }
        return {
            "project": project,
            "profile_found": False,
            "profile_path": str(profile_path) if profile_path else None,
            "profile": {},
            "safe_summary": safe_summary,
            "reason": "No configured project support profile was found.",
        }

    def get_support_coverage_summary(self) -> dict[str, Any]:
        """Return read-only coverage for configured support knowledge."""

        projects = self._coverage_projects()
        project_rows = [self._project_coverage(project) for project in projects]
        return {
            "project_count": len(project_rows),
            "projects": project_rows,
            "projects_missing_clickhouse_mapping": [
                item["project"]
                for item in project_rows
                if not item["has_clickhouse_mapping"]
            ],
            "projects_missing_project_rules": [
                item["project"] for item in project_rows if not item["has_project_rules"]
            ],
            "projects_missing_project_templates": [
                item["project"] for item in project_rows if not item["has_project_templates"]
            ],
            "projects_missing_project_profiles": [
                item["project"] for item in project_rows if not item["has_project_profile"]
            ],
            "generic_rules_path": str(Path(self.config.rules_path).expanduser()),
            "generic_templates_dir": str(Path(self.config.templates_dir).expanduser()),
            "label_suffix_case_type_count": len(self.label_suffix_by_case_type),
        }

    def _coverage_projects(self) -> list[str]:
        projects = set(self.project_label_names)
        projects.update(self.config.project_rules_paths)
        projects.update(self.config.project_templates_dirs)
        projects.update(self.config.project_profiles_paths)
        projects.update(self.clickhouse_project_case_type_tables)
        return sorted(project for project in projects if project)

    def _project_coverage(self, project: str) -> dict[str, Any]:
        project_rules_path = self.config.project_rules_paths.get(project)
        project_templates_dir = self.config.project_templates_dirs.get(project)
        profile_path = self._profile_path(project)
        clickhouse_tables = self.clickhouse_project_case_type_tables.get(project, {})
        has_rules = bool(
            project_rules_path and Path(project_rules_path).expanduser().exists()
        )
        has_templates = bool(
            project_templates_dir and Path(project_templates_dir).expanduser().exists()
        )
        has_profile = bool(profile_path and profile_path.exists())
        return {
            "project": project,
            "has_clickhouse_mapping": bool(clickhouse_tables),
            "clickhouse_case_types": sorted(clickhouse_tables),
            "has_project_rules": has_rules,
            "project_rules_path": project_rules_path,
            "has_project_templates": has_templates,
            "project_templates_dir": project_templates_dir,
            "has_project_profile": has_profile,
            "project_profile_path": str(profile_path) if profile_path else None,
            "uses_generic_rules_only": not has_rules,
            "safe_log_queries_only": not bool(clickhouse_tables),
            "label_suffix_by_case_type": self.label_suffix_by_case_type,
        }

    def get_reply_template(
        self,
        template_id: str,
        language: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Return a reply template by id and language."""

        if not SAFE_ID_RE.fullmatch(template_id):
            raise ToolResolutionError("template_id must be a simple identifier")
        requested_language = _normalize_template_language(
            language or self.default_language
        )
        if not SAFE_LANGUAGE_RE.fullmatch(requested_language):
            raise ToolResolutionError("language must be a simple identifier")

        base = self._templates_dir(project)
        default_base = Path(self.config.templates_dir).expanduser()
        legacy_hit = next(
            (item for item in self._load_legacy_templates() if item.id == template_id),
            None,
        )
        candidates: list[Path] = []
        if legacy_hit is not None:
            candidates.append(resolve_project_path(legacy_hit.body_path))
        candidates.extend(
            [
                base / requested_language / f"{template_id}.md",
                base / self.default_language / f"{template_id}.md",
                default_base / requested_language / f"{template_id}.md",
                default_base / self.default_language / f"{template_id}.md",
            ]
        )
        for path in candidates:
            if path.exists():
                resolved_language = path.parent.name
                language_fallback = resolved_language != requested_language
                payload: dict[str, Any] = {
                    "project": project,
                    "template_id": template_id,
                    "requested_language": requested_language,
                    "language": resolved_language,
                    "path": str(path),
                    "body": path.read_text(encoding="utf-8"),
                    "language_fallback": language_fallback,
                }
                if language_fallback:
                    payload["guidance"] = (
                        f"No template exists for requested_language={requested_language!r}. "
                        "Use this body as reference and write the Gmail draft in "
                        "detected_language without calling get_reply_template again."
                    )
                return payload

        suggestions: list[str] = []
        if template_id.startswith("feature_request"):
            suggestions.append("feature_request_ack")
        hint = (
            f" Try get_reply_template with {suggestions!r} and adapt to detected_language."
            if suggestions
            else " Write the draft in detected_language and call save_case_state."
        )
        raise ToolResolutionError(f"Reply template {template_id!r} was not found.{hint}")
