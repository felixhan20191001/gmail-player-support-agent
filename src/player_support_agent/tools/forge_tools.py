"""Forge ToolDef builders for the player support agent tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from forge import ToolDef, ToolSpec

from .clickhouse_tools import ClickHouseTools
from .config import SupportAgentConfig
from .decision_tools import DecisionTools
from .gmail_tools import GmailTools
from .notify_tools import NotifyTools
from .rule_tools import RuleTools
from .state_tools import StateTools
from .tool_shared_state import ToolSharedState


class EmptyParams(BaseModel):
    pass


class ListNewFeedbackEmailsParams(BaseModel):
    max_results: int = Field(default=10, ge=1, le=100)
    query: str | None = None


class ListUnreadInboxEmailsParams(BaseModel):
    max_results: int = Field(default=25, ge=1, le=100)
    query: str | None = None
    snippet_chars: int = Field(default=240, ge=40, le=500)


class ListUnreadProjectEmailsParams(BaseModel):
    max_results_per_label: int = Field(default=10, ge=1, le=50)
    project_labels: list[str] | None = None


class ReadEmailThreadParams(BaseModel):
    thread_id: str


class ApplyExistingGmailLabelsParams(BaseModel):
    message_ids: list[str]
    label_names: list[str]


class MarkGmailMessagesReadParams(BaseModel):
    message_ids: list[str]


class CreateGmailDraftParams(BaseModel):
    to: str
    subject: str
    body: str
    thread_id: str | None = None
    in_reply_to_message_id: str | None = None


class GetClickHouseSchemaParams(BaseModel):
    case_type: str
    project: str | None = None


class ValidateClickHouseSqlParams(BaseModel):
    sql: str
    player_id: str
    time_window_start: str
    time_window_end: str
    case_type: str
    project: str | None = None


class QueryClickHouseParams(ValidateClickHouseSqlParams):
    pass


class SummarizeBehaviorLogsParams(BaseModel):
    rows: list[dict[str, Any]]
    time_column: str = "event_time"
    event_column: str = "event_name"


class GetRelevantSupportRulesParams(BaseModel):
    case_type: str
    email_text: str
    project: str | None = None
    max_rules: int | None = Field(default=None, ge=1, le=20)
    include_case_defaults: bool = True


class SearchLegacyReplyTemplatesParams(BaseModel):
    case_type: str
    email_text: str
    project: str | None = None
    max_templates: int | None = Field(default=None, ge=1, le=20)
    include_case_defaults: bool = True


class GetReplyTemplateParams(BaseModel):
    template_id: str
    language: str | None = None
    project: str | None = None


class GetSupportKnowledgeSummaryParams(BaseModel):
    project: str | None = None


class GetProjectSupportProfileParams(BaseModel):
    project: str


class GetSupportEvidenceCatalogParams(BaseModel):
    project: str
    case_type: str


class GetRemoveAdsInvestigationPlaybookParams(BaseModel):
    project: str
    case_type: str = "ads_after_purchase"


class GetCoinFrenzyInvestigationPlaybookParams(BaseModel):
    project: str
    case_type: str = "pass_purchase_misunderstanding"


class AssessRemoveAdsLogEvidenceParams(BaseModel):
    purchase_success_found: bool
    remove_ads_purchase_time: str | None = None
    remove_ads_product_id: str | None = None
    latest_interstitial_time: str | None = None
    purchase_click_only: bool = False
    interstitial_after_purchase: bool | None = None


class AssessCoinFrenzyLogEvidenceParams(BaseModel):
    purchase_success_found: bool
    purchase_product_id: str | None = None
    purchase_click_only: bool = False


class QuerySupportEvidenceParams(BaseModel):
    project: str
    case_type: str
    player_id: str
    time_window_start: str
    time_window_end: str
    evidence_kind: str


class ExtractFeedbackClaimParams(BaseModel):
    case_type: str
    summary: str
    project: str | None = None
    available_label_names: list[str] | None = None
    player_id: str | None = None
    server_id: str | None = None
    character_name: str | None = None
    time_window_start: str | None = None
    time_window_end: str | None = None
    requested_action: str | None = None
    missing_fields: list[str] | None = None
    detected_language: str | None = None
    language_source_text: str | None = None


class ResolvePlayerIdentityParams(BaseModel):
    player_id: str | None = None
    server_id: str | None = None
    email: str | None = None
    character_name: str | None = None


class AssessClaimCredibilityParams(BaseModel):
    verdict: Literal["supported", "contradicted", "inconclusive"]
    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: Literal["low", "medium", "high"]
    evidence: list[str]
    missing_data: list[str] | None = None


class DecideSupportActionParams(BaseModel):
    case_type: str
    verdict: Literal["supported", "contradicted", "inconclusive"]
    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: Literal["low", "medium", "high"]
    missing_fields: list[str] | None = None
    applied_rule_ids: list[str] | None = None
    rule_action: str | None = None
    rule_human_review: bool | None = None
    evidence_recommended_action: str | None = None


class ReviewReplyDraftParams(BaseModel):
    project: str | None = None
    case_type: str
    detected_language: str | None = None
    claim_summary: str
    matched_rule_ids: list[str] | None = None
    evidence_summary: dict[str, Any]
    decision: dict[str, Any]
    draft_body: str


class CreateHumanHandoffSummaryParams(BaseModel):
    case_id: str
    email_subject: str
    player_summary: str
    claim_summary: str
    evidence_summary: dict[str, Any]
    ai_recommendation: str
    draft_reply: str | None = None


class NotifyHumanSupportParams(BaseModel):
    case_id: str
    subject: str
    summary_text: str
    priority: str = "normal"


class GetCaseStateParams(BaseModel):
    case_id: str


class SaveCaseStateParams(BaseModel):
    case_id: str
    status: str
    data: dict[str, Any]


class WriteAuditLogParams(BaseModel):
    case_id: str
    event_type: str
    payload: dict[str, Any]


def _tool(
    name: str,
    description: str,
    params: type[BaseModel],
    fn,
    prerequisites: list[str | dict[str, str]] | None = None,
) -> ToolDef:
    return ToolDef(
        spec=ToolSpec(name=name, description=description, parameters=params),
        callable=fn,
        prerequisites=prerequisites or [],
    )


ToolSurface = Literal["chat", "auto", "cleanup"]


AUTO_TOOL_NAMES = {
    "read_email_thread",
    "get_existing_gmail_labels",
    "apply_existing_gmail_labels",
    "mark_gmail_messages_read",
    "create_gmail_draft",
    "get_clickhouse_schema",
    "validate_clickhouse_sql",
    "query_clickhouse",
    "summarize_behavior_logs",
    "get_support_evidence_catalog",
    "query_support_evidence",
    "get_remove_ads_investigation_playbook",
    "get_coin_frenzy_investigation_playbook",
    "get_relevant_support_rules",
    "get_project_support_profile",
    "get_reply_template",
    "extract_feedback_claim",
    "resolve_player_identity",
    "assess_remove_ads_log_evidence",
    "assess_coin_frenzy_log_evidence",
    "assess_claim_credibility",
    "decide_support_action",
    "review_reply_draft",
    "create_human_handoff_summary",
    "notify_human_support",
    "save_case_state",
}


CLEANUP_TOOL_NAMES = {
    "get_existing_gmail_labels",
    "apply_existing_gmail_labels",
    "mark_gmail_messages_read",
    "save_case_state",
}


POST_DECISION_RESTART_TOOL_NAMES = {
    "read_email_thread",
    "get_existing_gmail_labels",
    "get_project_support_profile",
    "extract_feedback_claim",
    "get_relevant_support_rules",
    "resolve_player_identity",
    "assess_claim_credibility",
    "get_support_evidence_catalog",
    "query_support_evidence",
    "get_clickhouse_schema",
    "validate_clickhouse_sql",
    "query_clickhouse",
    "summarize_behavior_logs",
    "get_remove_ads_investigation_playbook",
    "get_coin_frenzy_investigation_playbook",
    "assess_remove_ads_log_evidence",
    "assess_coin_frenzy_log_evidence",
}


POST_DECISION_NEXT_STEPS = (
    "decide_support_action already returned. Do not restart read/extract/rules/"
    "evidence tools. Continue the finish path now: optionally call "
    "get_reply_template once if a template is still needed, then "
    "review_reply_draft and create_gmail_draft for draft actions, or "
    "create_human_handoff_summary and notify_human_support for human handoff, "
    "then apply_existing_gmail_labels, mark_gmail_messages_read, and "
    "save_case_state as the final tool call."
)


def _with_post_decision_guard(
    name: str,
    tool: ToolDef,
    shared_state: ToolSharedState,
) -> ToolDef:
    original = tool.callable

    def guarded_callable(*args, **kwargs):
        if name in POST_DECISION_RESTART_TOOL_NAMES and shared_state.has_support_decision():
            return {
                "blocked": True,
                "tool": name,
                "error": "decide_support_action already returned for this case.",
                "decision": shared_state.get_last_support_decision(),
                "next_steps": POST_DECISION_NEXT_STEPS,
            }
        result = original(*args, **kwargs)
        if name == "decide_support_action" and isinstance(result, dict):
            shared_state.set_last_support_decision(result)
        return result

    return ToolDef(
        spec=tool.spec,
        callable=guarded_callable,
        prerequisites=tool.prerequisites,
    )


def _with_auto_post_decision_guards(
    tools: dict[str, ToolDef],
    shared_state: ToolSharedState,
) -> dict[str, ToolDef]:
    return {
        name: _with_post_decision_guard(name, tool, shared_state)
        for name, tool in tools.items()
    }


def build_tool_defs(
    config: SupportAgentConfig,
    *,
    surface: ToolSurface = "chat",
) -> dict[str, ToolDef]:
    """Build the MVP player-support ToolDef map for a Forge Workflow."""

    compact_results = surface in {"auto", "cleanup"}
    shared_state = ToolSharedState()
    gmail = GmailTools(
        config.gmail,
        shared_state=shared_state,
        compact_results=compact_results,
    )
    ch = ClickHouseTools(
        config.clickhouse,
        remove_ads_investigation_path=config.knowledge.remove_ads_investigation_path,
        coin_frenzy_investigation_path=config.knowledge.coin_frenzy_investigation_path,
        compact_results=compact_results,
    )
    decisions = DecisionTools(config.policy, shared_state=shared_state)
    notify = NotifyTools(config.notify)
    rules = RuleTools(
        config.knowledge,
        default_language=config.policy.default_language,
        project_label_names=config.gmail.project_label_names,
        clickhouse_project_case_type_tables=config.clickhouse.project_case_type_tables,
        label_suffix_by_case_type=config.policy.label_suffix_by_case_type,
        compact_results=compact_results,
    )
    state = StateTools(config.state)

    tools = {
        "list_new_feedback_emails": _tool(
            "list_new_feedback_emails",
            "List Gmail player-feedback messages using the configured or supplied query.",
            ListNewFeedbackEmailsParams,
            gmail.list_new_feedback_emails,
        ),
        "list_unread_inbox_emails": _tool(
            "list_unread_inbox_emails",
            (
                "List unread inbox Gmail messages across labels and return safe "
                "metadata, snippets, label names, and inferred project labels. "
                "This is read-only and does not fetch full bodies."
            ),
            ListUnreadInboxEmailsParams,
            gmail.list_unread_inbox_emails,
        ),
        "list_unread_project_emails": _tool(
            "list_unread_project_emails",
            (
                "Discover unread inbox messages under existing Gmail project "
                "parent labels and return message ids plus project label hints."
            ),
            ListUnreadProjectEmailsParams,
            gmail.list_unread_project_emails,
        ),
        "read_email_thread": _tool(
            "read_email_thread",
            (
                "Read a full Gmail thread and normalize every message body, "
                "including prior player emails and prior support replies. "
                "Read the full thread chronologically, then treat only the "
                "latest player-authored inbound message as the active request "
                "to answer."
            ),
            ReadEmailThreadParams,
            gmail.read_email_thread,
        ),
        "get_existing_gmail_labels": _tool(
            "get_existing_gmail_labels",
            "Return existing Gmail labels. Never creates labels.",
            EmptyParams,
            gmail.get_existing_gmail_labels,
        ),
        "apply_existing_gmail_labels": _tool(
            "apply_existing_gmail_labels",
            "Apply only existing, preconfigured Gmail labels to messages.",
            ApplyExistingGmailLabelsParams,
            gmail.apply_existing_gmail_labels,
            prerequisites=["get_existing_gmail_labels"],
        ),
        "mark_gmail_messages_read": _tool(
            "mark_gmail_messages_read",
            (
                "Mark Gmail messages as read by removing the UNREAD label. "
                "Use after apply_existing_gmail_labels when decide_support_action "
                "returns skip_label_only (e.g. no_content or apply_label_only rules "
                "such as ad_promo_mismatch_label_only)."
            ),
            MarkGmailMessagesReadParams,
            gmail.mark_gmail_messages_read,
            prerequisites=["apply_existing_gmail_labels"],
        ),
        "create_gmail_draft": _tool(
            "create_gmail_draft",
            "Create a Gmail draft reply. This tool never sends email.",
            CreateGmailDraftParams,
            gmail.create_gmail_draft,
            prerequisites=["decide_support_action", "review_reply_draft"],
        ),
        "get_clickhouse_schema": _tool(
            "get_clickhouse_schema",
            "Return whitelisted ClickHouse tables and columns for the project and case type.",
            GetClickHouseSchemaParams,
            ch.get_clickhouse_schema,
        ),
        "validate_clickhouse_sql": _tool(
            "validate_clickhouse_sql",
            (
                "Optional preview check that SQL is read-only, scoped, limited, and "
                "whitelisted. query_clickhouse re-validates internally. If ok:false, "
                "read reason and either fix SQL once or skip SQL and continue the "
                "workflow without logs. Do not call this tool repeatedly with tiny "
                "SQL tweaks."
            ),
            ValidateClickHouseSqlParams,
            ch.validate_clickhouse_sql,
            prerequisites=["get_clickhouse_schema"],
        ),
        "query_clickhouse": _tool(
            "query_clickhouse",
            (
                "Execute a validated ClickHouse SELECT and return a compact summary. "
                "Call get_clickhouse_schema first. Do not batch this with "
                "validate_clickhouse_sql in the same turn."
            ),
            QueryClickHouseParams,
            ch.query_clickhouse,
            prerequisites=["get_clickhouse_schema"],
        ),
        "summarize_behavior_logs": _tool(
            "summarize_behavior_logs",
            "Summarize ClickHouse behavior-log rows into compact evidence.",
            SummarizeBehaviorLogsParams,
            ch.summarize_behavior_logs,
            prerequisites=["query_clickhouse"],
        ),
        "get_support_evidence_catalog": _tool(
            "get_support_evidence_catalog",
            (
                "Return configured support evidence recipes for a project and case type. "
                "When available=false, follow next_steps and skip ClickHouse unless a "
                "matched rule requires logs."
            ),
            GetSupportEvidenceCatalogParams,
            ch.get_support_evidence_catalog,
        ),
        "query_support_evidence": _tool(
            "query_support_evidence",
            (
                "Run a configured support evidence recipe through ClickHouse validation "
                "and return a compact evidence judgment."
            ),
            QuerySupportEvidenceParams,
            ch.query_support_evidence,
            prerequisites=["get_support_evidence_catalog"],
        ),
        "get_remove_ads_investigation_playbook": _tool(
            "get_remove_ads_investigation_playbook",
            (
                "Return read-only remove-ads investigation steps, query hints, example SQL, "
                "and outcome branches for ads_after_purchase. Does not execute SQL."
            ),
            GetRemoveAdsInvestigationPlaybookParams,
            ch.get_remove_ads_investigation_playbook,
            prerequisites=["extract_feedback_claim"],
        ),
        "get_coin_frenzy_investigation_playbook": _tool(
            "get_coin_frenzy_investigation_playbook",
            (
                "Return read-only pass / Coin Frenzy / starlight pass investigation steps, query hints, "
                "example SQL, and outcome branches for 'bought pass but did not receive rewards' cases. "
                "Does not execute SQL."
            ),
            GetCoinFrenzyInvestigationPlaybookParams,
            ch.get_coin_frenzy_investigation_playbook,
            prerequisites=["extract_feedback_claim"],
        ),
        "get_relevant_support_rules": _tool(
            "get_relevant_support_rules",
            (
                "Retrieve matching customer-support rules for this project/case type "
                "and email wording. email_text must be copied verbatim from "
                "read_email_thread. Returns has_strong_match, recommended_rule_id, "
                "and guidance when only weak case_type matches exist. Also returns "
                "matched_legacy_templates from the imported human reply library "
                "when relevant."
            ),
            GetRelevantSupportRulesParams,
            rules.get_relevant_support_rules,
            prerequisites=["extract_feedback_claim"],
        ),
        "search_legacy_reply_templates": _tool(
            "search_legacy_reply_templates",
            (
                "Search imported legacy human reply templates by project, case type, "
                "and email wording. Use reply_template ids with get_reply_template."
            ),
            SearchLegacyReplyTemplatesParams,
            rules.search_legacy_reply_templates,
            prerequisites=["extract_feedback_claim"],
        ),
        "get_support_knowledge_summary": _tool(
            "get_support_knowledge_summary",
            "Return a read-only summary of configured support rules and templates.",
            GetSupportKnowledgeSummaryParams,
            rules.get_support_knowledge_summary,
        ),
        "get_support_coverage_summary": _tool(
            "get_support_coverage_summary",
            "Return read-only coverage of project rules, templates, profiles, and log mappings.",
            EmptyParams,
            rules.get_support_coverage_summary,
        ),
        "get_project_support_profile": _tool(
            "get_project_support_profile",
            "Return a configured project support profile or a safe derived summary.",
            GetProjectSupportProfileParams,
            rules.get_project_support_profile,
            prerequisites=["get_existing_gmail_labels"],
        ),
        "get_reply_template": _tool(
            "get_reply_template",
            (
                "Retrieve a configured project-specific customer-facing reply "
                "template by id. Call at most once per template_id per message. "
                "When language_fallback=true, adapt the body into detected_language "
                "and continue without retrying other languages."
            ),
            GetReplyTemplateParams,
            rules.get_reply_template,
            prerequisites=["get_relevant_support_rules"],
        ),
        "extract_feedback_claim": _tool(
            "extract_feedback_claim",
            "Normalize the feedback claim extracted from the email thread. "
            "Do not list server_id or character_name in missing_fields; only player_id may be required.",
            ExtractFeedbackClaimParams,
            decisions.extract_feedback_claim,
        ),
        "resolve_player_identity": _tool(
            "resolve_player_identity",
            "Check whether the email contains enough player identity fields. "
            "Only player_id is required; server_id and character_name are never required.",
            ResolvePlayerIdentityParams,
            decisions.resolve_player_identity,
            prerequisites=["extract_feedback_claim"],
        ),
        "assess_remove_ads_log_evidence": _tool(
            "assess_remove_ads_log_evidence",
            (
                "Interpret structured remove-ads log findings and recommend reply branch. "
                "Call after purchase and interstitial ClickHouse queries."
            ),
            AssessRemoveAdsLogEvidenceParams,
            decisions.assess_remove_ads_log_evidence,
            prerequisites=["resolve_player_identity"],
        ),
        "assess_coin_frenzy_log_evidence": _tool(
            "assess_coin_frenzy_log_evidence",
            (
                "Interpret structured pass / Coin Frenzy purchase findings (including starlight pass) "
                "and recommend reply branch. Call after purchase ClickHouse query when "
                "pass_purchase_misunderstanding or coin_frenzy rule matches."
            ),
            AssessCoinFrenzyLogEvidenceParams,
            decisions.assess_coin_frenzy_log_evidence,
            prerequisites=["resolve_player_identity"],
        ),
        "assess_claim_credibility": _tool(
            "assess_claim_credibility",
            "Normalize a credibility judgment based on evidence and risk.",
            AssessClaimCredibilityParams,
            decisions.assess_claim_credibility,
            prerequisites=["resolve_player_identity"],
        ),
        "decide_support_action": _tool(
            "decide_support_action",
            (
                "Decide whether to create a draft, ask for information, hand off, or "
                "apply labels only. Pass rule_action and applied_rule_ids from "
                "get_relevant_support_rules; tools do not auto-classify emails or "
                "pick reply templates. evidence_recommended_action is honored only "
                "when applied_rule_ids is non-empty (after assess_remove_ads_log_evidence or "
                "assess_coin_frenzy_log_evidence)."
            ),
            DecideSupportActionParams,
            decisions.decide_support_action,
            prerequisites=["assess_claim_credibility"],
        ),
        "review_reply_draft": _tool(
            "review_reply_draft",
            "Review a proposed customer-facing draft for generic safety before draft creation.",
            ReviewReplyDraftParams,
            decisions.review_reply_draft,
            prerequisites=["decide_support_action"],
        ),
        "create_human_handoff_summary": _tool(
            "create_human_handoff_summary",
            "Create a compact summary for human support review.",
            CreateHumanHandoffSummaryParams,
            notify.create_human_handoff_summary,
            prerequisites=["decide_support_action"],
        ),
        "notify_human_support": _tool(
            "notify_human_support",
            "Notify human support through the configured notification channel.",
            NotifyHumanSupportParams,
            notify.notify_human_support,
            prerequisites=["create_human_handoff_summary"],
        ),
        "get_case_state": _tool(
            "get_case_state",
            "Read local processing state for this case.",
            GetCaseStateParams,
            state.get_case_state,
        ),
        "save_case_state": _tool(
            "save_case_state",
            "Persist local processing state for this case.",
            SaveCaseStateParams,
            state.save_case_state,
        ),
        "write_audit_log": _tool(
            "write_audit_log",
            "Append a JSONL audit event for traceability.",
            WriteAuditLogParams,
            state.write_audit_log,
        ),
    }
    if surface == "chat":
        return tools
    if surface == "auto":
        auto_tools = {name: tools[name] for name in tools if name in AUTO_TOOL_NAMES}
        return _with_auto_post_decision_guards(auto_tools, shared_state)
    if surface == "cleanup":
        return {name: tools[name] for name in tools if name in CLEANUP_TOOL_NAMES}
    raise ValueError(f"Unknown tool surface: {surface}")
