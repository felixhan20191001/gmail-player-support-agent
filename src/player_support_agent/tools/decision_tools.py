"""Deterministic decision helpers for the support workflow."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from .config import SupportPolicyConfig

_REMOVE_ADS_PRODUCT_RE = re.compile(
    r"remove\s*ads?|no\s*ads?|noads|ad\s*free|removead",
    re.IGNORECASE,
)
_COIN_FRENZY_PRODUCT_RE = re.compile(
    r"coin[._-]?frenzy|coinfrenzy|pass|starlight|starlight\.pass|com\.black\.hole.*pass",
    re.IGNORECASE,
)
_COIN_FRENZY_ACTIVITY_CASE_TYPES = frozenset(
    {
        "pass_purchase_misunderstanding",
        "gameplay_misunderstanding",
        "payment",
    }
)
_ADS_AFTER_PURCHASE_PRIMARY_SUFFIX = "去广告后有广告"
_ADS_AFTER_PURCHASE_FALLBACK_SUFFIX = "内购问题"
_EVIDENCE_ACTION_ALIASES = {
    "ask_for_order_id_or_receipt": "draft_for_review",
    "request_order_id": "draft_for_review",
}
_ORDER_EVIDENCE_MISSING_FIELD_KEYS = frozenset(
    {
        "order_id",
        "order_id_or_purchase_receipt",
        "purchase_receipt",
        "google_order_number",
        "appstore_payment_record",
    }
)
_RULE_ID_ALIASES_BY_CASE_TYPE: dict[str, dict[str, str]] = {
    "feature_request": {
        "feature_request_general_acknowledge": "feature_request_ack",
        "feature_request_acknowledge": "feature_request_ack",
    },
}


def _normalize_applied_rule_ids(
    case_type: str,
    applied_rule_ids: list[str] | None,
) -> list[str]:
    aliases = _RULE_ID_ALIASES_BY_CASE_TYPE.get(case_type, {})
    normalized = _dedupe(
        aliases.get(rule_id, rule_id) for rule_id in (applied_rule_ids or [])
    )
    if normalized:
        return normalized
    if case_type == "feature_request":
        return ["feature_request_ack"]
    return []


def _label_belongs_to_project(label: str, project: str) -> bool:
    return label == project or label.startswith(f"{project}/")


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


_OPTIONAL_IDENTITY_FIELDS = frozenset({"server_id", "character_name"})
_DISALLOWED_MISSING_FIELD_ALIASES = _OPTIONAL_IDENTITY_FIELDS | {
    "server",
    "character",
    "charactername",
    "nickname",
    "角色名",
    "角色名称",
    "服务器",
    "区服",
}
_IDENTITY_ASK_PATTERNS = (
    "server id",
    "server-id",
    "character name",
    "in-game name",
    "nickname in the game",
    "game's settings menu",
    "settings menu",
    "服务器",
    "区服",
    "角色名",
    "角色名称",
    "游戏内昵称",
)


def _normalize_missing_field_key(value: str) -> str:
    return str(value or "").strip().casefold().replace(" ", "_").replace("-", "_")


def _parse_evidence_time(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_remove_ads_product(product_id: str | None) -> bool:
    if not product_id:
        return False
    return bool(_REMOVE_ADS_PRODUCT_RE.search(product_id))


def _is_coin_frenzy_product(product_id: str | None) -> bool:
    """Returns true for pass-like or coin frenzy products (starlight pass, coin.frenzy etc.)."""
    if not product_id:
        return False
    return bool(_COIN_FRENZY_PRODUCT_RE.search(product_id))


def _normalize_evidence_recommended_action(value: str | None) -> str | None:
    if not value:
        return None
    return _EVIDENCE_ACTION_ALIASES.get(value.strip(), value.strip())


def _is_order_evidence_missing_field(value: str) -> bool:
    key = _normalize_missing_field_key(value)
    if key in _ORDER_EVIDENCE_MISSING_FIELD_KEYS:
        return True
    return "order" in key and ("receipt" in key or "purchase" in key or key == "order_id")


def _ads_after_purchase_issue_labels(
    project: str,
    available_label_names: set[str] | None,
) -> tuple[list[str], str | None]:
    candidates = [
        f"{project}/{_ADS_AFTER_PURCHASE_PRIMARY_SUFFIX}",
        f"{project}/{_ADS_AFTER_PURCHASE_FALLBACK_SUFFIX}",
    ]
    if available_label_names is None:
        return [project, candidates[0]], "preferred_suffix_unverified"
    labels = [project]
    selected: str | None = None
    selection_reason: str | None = None
    for candidate in candidates:
        if candidate in available_label_names:
            labels.append(candidate)
            selected = candidate
            selection_reason = (
                "primary_suffix"
                if candidate.endswith(_ADS_AFTER_PURCHASE_PRIMARY_SUFFIX)
                else "fallback_payment_suffix"
            )
            break
    return labels, selection_reason if selected else "no_matching_issue_suffix"


def _filter_disallowed_missing_fields(values: list[str] | None) -> list[str]:
    filtered: list[str] = []
    for value in values or []:
        key = _normalize_missing_field_key(value)
        if key in _DISALLOWED_MISSING_FIELD_ALIASES:
            continue
        filtered.append(value)
    return filtered


def _draft_asks_for_disallowed_identity(draft_body: str) -> bool:
    text = draft_body.casefold()
    return any(pattern in text for pattern in _IDENTITY_ASK_PATTERNS)


class DecisionTools:
    """Small policy helpers that keep risky choices explicit."""

    def __init__(self, config: SupportPolicyConfig) -> None:
        self.config = config

    def extract_feedback_claim(
        self,
        case_type: str,
        summary: str,
        project: str | None = None,
        available_label_names: list[str] | None = None,
        player_id: str | None = None,
        server_id: str | None = None,
        character_name: str | None = None,
        time_window_start: str | None = None,
        time_window_end: str | None = None,
        requested_action: str | None = None,
        missing_fields: list[str] | None = None,
        detected_language: str | None = None,
        language_source_text: str | None = None,
    ) -> dict[str, Any]:
        """Normalize the model's extracted claim into a stable shape."""

        recommended_labels = self.config.label_by_case_type.get(case_type, [])
        if project and case_type != "no_content":
            recommended_labels = [
                label
                for label in recommended_labels
                if _label_belongs_to_project(label, project)
            ]
        label_selection_reason: str | None = None
        if project and case_type == "ads_after_purchase":
            available = (
                set(available_label_names) if available_label_names is not None else None
            )
            recommended_labels, label_selection_reason = _ads_after_purchase_issue_labels(
                project,
                available,
            )
        elif project and case_type != "no_content":
            suffixes = self.config.label_suffix_by_case_type.get(case_type, [])
            if suffixes:
                recommended_labels += [project]
                recommended_labels += [
                    f"{project}/{suffix.strip('/')}"
                    for suffix in suffixes
                    if suffix.strip("/")
                ]
            recommended_labels = _dedupe(recommended_labels)
            if available_label_names is not None:
                available = set(available_label_names)
                recommended_labels = [
                    label for label in recommended_labels if label in available
                ]
        else:
            recommended_labels = _dedupe(recommended_labels)
            if available_label_names is not None:
                available = set(available_label_names)
                recommended_labels = [
                    label for label in recommended_labels if label in available
                ]

        return {
            "project": project,
            "case_type": case_type,
            "summary": summary,
            "player_id": player_id,
            "server_id": server_id,
            "character_name": character_name,
            "time_window_start": time_window_start,
            "time_window_end": time_window_end,
            "requested_action": requested_action,
            "missing_fields": _filter_disallowed_missing_fields(missing_fields),
            "detected_language": detected_language,
            "language_source_text": language_source_text,
            "recommended_labels": recommended_labels,
            "label_selection_reason": label_selection_reason,
        }

    def resolve_player_identity(
        self,
        player_id: str | None = None,
        server_id: str | None = None,
        email: str | None = None,
        character_name: str | None = None,
    ) -> dict[str, Any]:
        """Resolve whether the email contains enough identity information.

        Production systems can replace this with a real account lookup tool.
        Our games do not use multi-server workflows, so only player_id is required.
        """

        missing: list[str] = []
        if not player_id:
            missing.append("player_id")
        return {
            "resolved": not missing,
            "player_id": player_id,
            "server_id": server_id,
            "email": email,
            "character_name": character_name,
            "missing_fields": missing,
            "server_id_required": False,
            "character_name_required": False,
            "policy_notes": [
                "Games do not use multi-server support workflows.",
                "Never ask players for server ID or in-game character name.",
                "player_id or user_id from the email is sufficient when present.",
            ],
        }

    def assess_remove_ads_log_evidence(
        self,
        purchase_success_found: bool,
        remove_ads_purchase_time: str | None = None,
        remove_ads_product_id: str | None = None,
        latest_interstitial_time: str | None = None,
        purchase_click_only: bool = False,
        interstitial_after_purchase: bool | None = None,
    ) -> dict[str, Any]:
        """Recommend remove-ads reply branch from structured log findings (no SQL)."""

        reasoning: list[str] = []
        if purchase_click_only and not purchase_success_found:
            return {
                "branch_id": "purchase_click_only",
                "verdict": "inconclusive",
                "confidence": 0.55,
                "recommended_template_id": "remove_ads_no_order_request_order_id",
                "recommended_action": "draft_for_review",
                "human_review": True,
                "reasoning": [
                    "Only purchase-click interaction found without successful remove-ads purchase.",
                ],
            }

        if not purchase_success_found:
            return {
                "branch_id": "no_purchase_success",
                "verdict": "inconclusive",
                "confidence": 0.6,
                "recommended_template_id": "remove_ads_no_order_request_order_id",
                "recommended_action": "draft_for_review",
                "human_review": True,
                "reasoning": [
                    "No successful remove-ads purchase record found in supplied log findings.",
                ],
            }

        if remove_ads_product_id and not _is_remove_ads_product(remove_ads_product_id):
            reasoning.append(
                f"Purchase product_id {remove_ads_product_id!r} does not look like remove-ads."
            )

        purchase_time = _parse_evidence_time(remove_ads_purchase_time)
        interstitial_time = _parse_evidence_time(latest_interstitial_time)

        forced_after_purchase = interstitial_after_purchase
        if forced_after_purchase is None and purchase_time and interstitial_time:
            forced_after_purchase = interstitial_time >= purchase_time
            reasoning.append(
                "Compared latest AdShow_Inter time against remove-ads purchase time."
            )
        elif forced_after_purchase is None and purchase_time and not interstitial_time:
            forced_after_purchase = False
            reasoning.append(
                "No interstitial ad show found after remove-ads purchase window."
            )

        if forced_after_purchase:
            return {
                "branch_id": "forced_interstitial_after_purchase",
                "verdict": "contradicted",
                "confidence": 0.8,
                "recommended_template_id": "",
                "recommended_action": "handoff_human",
                "human_review": True,
                "reasoning": reasoning
                + [
                    "Interstitial ads still appear after remove-ads purchase; escalate to human support.",
                ],
            }

        return {
            "branch_id": "rv_only_after_purchase",
            "verdict": "supported",
            "confidence": 0.88,
            "recommended_template_id": "ads_after_purchase_rv_only_explanation",
            "recommended_action": "create_draft",
            "human_review": False,
            "reasoning": reasoning
            + [
                "Remove-ads purchase succeeded and forced interstitial ads stopped after purchase.",
                "Remaining ads are likely optional rewarded video placements.",
            ],
        }

    def assess_coin_frenzy_log_evidence(
        self,
        purchase_success_found: bool,
        purchase_product_id: str | None = None,
        purchase_click_only: bool = False,
    ) -> dict[str, Any]:
        """Recommend pass / Coin Frenzy reply branch from structured purchase findings (no SQL).
        Handles starlight pass, coin.frenzy and other pass-like products.
        """

        if purchase_click_only and not purchase_success_found:
            return {
                "branch_id": "purchase_click_only",
                "verdict": "inconclusive",
                "confidence": 0.55,
                "recommended_template_id": "no_purchase_record_reinstall_check",
                "recommended_action": "draft_for_review",
                "human_review": True,
                "reasoning": [
                    "Only purchase-click interaction found without successful pass purchase.",
                ],
            }

        if not purchase_success_found:
            return {
                "branch_id": "no_purchase_success",
                "verdict": "inconclusive",
                "confidence": 0.6,
                "recommended_template_id": "no_purchase_record_reinstall_check",
                "recommended_action": "draft_for_review",
                "human_review": True,
                "reasoning": [
                    "No successful pass-like purchase record found in supplied log findings.",
                ],
            }

        if _is_coin_frenzy_product(purchase_product_id):
            return {
                "branch_id": "coin_frenzy_purchase_confirmed",
                "verdict": "supported",
                "confidence": 0.88,
                "recommended_template_id": "coin_frenzy_task_reward_explanation",
                "recommended_action": "create_draft",
                "human_review": False,
                "reasoning": [
                    f"Successful pass/activity purchase found for product_id={purchase_product_id!r}.",
                    "Explain that pass rewards require progress/tasks and manual claim, not instant delivery.",
                ],
            }

        return {
            "branch_id": "other_product_purchase",
            "verdict": "inconclusive",
            "confidence": 0.65,
            "recommended_template_id": "",
            "recommended_action": "handoff_human",
            "human_review": True,
            "reasoning": [
                f"Purchase succeeded but product_id={purchase_product_id!r} is not pass-like.",
            ],
        }

    def assess_claim_credibility(
        self,
        verdict: Literal["supported", "contradicted", "inconclusive"],
        confidence: float,
        risk_level: Literal["low", "medium", "high"],
        evidence: list[str],
        missing_data: list[str] | None = None,
    ) -> dict[str, Any]:
        """Normalize credibility output and clamp confidence."""

        confidence = max(0.0, min(1.0, confidence))
        return {
            "verdict": verdict,
            "confidence": confidence,
            "risk_level": risk_level,
            "evidence": evidence,
            "missing_data": _filter_disallowed_missing_fields(missing_data),
        }

    def decide_support_action(
        self,
        case_type: str,
        verdict: Literal["supported", "contradicted", "inconclusive"],
        confidence: float,
        risk_level: Literal["low", "medium", "high"],
        missing_fields: list[str] | None = None,
        applied_rule_ids: list[str] | None = None,
        rule_action: str | None = None,
        rule_human_review: bool | None = None,
        evidence_recommended_action: str | None = None,
    ) -> dict[str, Any]:
        """Decide whether to draft a reply or hand off to a human."""

        evidence_recommended_action = _normalize_evidence_recommended_action(
            evidence_recommended_action
        )
        missing_fields = [
            field
            for field in (missing_fields or [])
            if field not in _OPTIONAL_IDENTITY_FIELDS
        ]
        if case_type in {"ads_after_purchase", *_COIN_FRENZY_ACTIVITY_CASE_TYPES} and (
            evidence_recommended_action in {"draft_for_review", "create_draft"}
        ):
            missing_fields = [
                field
                for field in missing_fields
                if not _is_order_evidence_missing_field(field)
            ]
        applied_rule_ids = _normalize_applied_rule_ids(case_type, applied_rule_ids)
        evidence_action_allowed = bool(applied_rule_ids)
        if evidence_action_allowed and evidence_recommended_action == "handoff_human":
            reason = (
                "Matched rule plus Coin Frenzy log evidence indicates human handoff"
                if case_type in _COIN_FRENZY_ACTIVITY_CASE_TYPES
                else "Matched rule plus remove-ads log evidence indicates human handoff"
            )
            return {
                "action": "handoff_human",
                "reason": reason,
                "allow_auto_draft": False,
                "requires_human": True,
                "applied_rule_ids": applied_rule_ids,
            }
        if (
            evidence_action_allowed
            and evidence_recommended_action == "create_draft"
            and case_type in {"ads_after_purchase", *_COIN_FRENZY_ACTIVITY_CASE_TYPES}
            and verdict == "supported"
            and confidence >= self.config.auto_draft_confidence_threshold
            and risk_level != "high"
        ):
            reason = (
                "Matched rule plus Coin Frenzy log evidence supports direct draft"
                if case_type in _COIN_FRENZY_ACTIVITY_CASE_TYPES
                else "Matched rule plus remove-ads log evidence supports direct draft"
            )
            return {
                "action": "create_draft",
                "reason": reason,
                "allow_auto_draft": True,
                "requires_human": False,
                "applied_rule_ids": applied_rule_ids,
            }
        if evidence_action_allowed and evidence_recommended_action == "draft_for_review":
            reason = (
                "Matched rule plus Coin Frenzy evidence missing; draft for review"
                if case_type in _COIN_FRENZY_ACTIVITY_CASE_TYPES
                else "Matched rule plus remove-ads evidence missing; draft for review"
            )
            return {
                "action": "draft_for_review",
                "reason": reason,
                "allow_auto_draft": True,
                "requires_human": True,
                "applied_rule_ids": applied_rule_ids,
            }
        if rule_action == "apply_label_only":
            reason = "Matched support rule requests label only without reply"
            return {
                "action": "skip_label_only",
                "reason": reason,
                "allow_auto_draft": False,
                "requires_human": False,
                "applied_rule_ids": applied_rule_ids,
            }
        high_risk = risk_level == "high" or case_type in self.config.high_risk_case_types
        rule_allows_direct_draft = (
            rule_action in {"draft_reply", "create_draft"}
            and rule_human_review is False
        )
        if missing_fields:
            return {
                "action": "draft_missing_info",
                "reason": f"Missing required fields: {', '.join(missing_fields)}",
                "allow_auto_draft": True,
                "requires_human": False,
                "applied_rule_ids": applied_rule_ids,
            }
        if (
            rule_allows_direct_draft
            and verdict == "supported"
            and confidence >= self.config.auto_draft_confidence_threshold
            and risk_level != "high"
        ):
            return {
                "action": "create_draft",
                "reason": "Specific support rule allows a direct draft with sufficient evidence",
                "allow_auto_draft": True,
                "requires_human": False,
                "applied_rule_ids": applied_rule_ids,
            }
        if high_risk:
            return {
                "action": "handoff_human",
                "reason": "High risk case type or high risk assessment",
                "allow_auto_draft": False,
                "requires_human": True,
                "applied_rule_ids": applied_rule_ids,
            }
        if (
            rule_allows_direct_draft
            and case_type in self.config.auto_draft_without_logs_case_types
        ):
            return {
                "action": "create_draft",
                "reason": (
                    "Support rule allows a template-based reply without log evidence "
                    f"for {case_type}"
                ),
                "allow_auto_draft": True,
                "requires_human": False,
                "applied_rule_ids": applied_rule_ids,
            }
        if verdict == "inconclusive" or confidence < self.config.human_review_confidence_threshold:
            return {
                "action": "handoff_human",
                "reason": "Evidence is inconclusive or confidence is too low",
                "allow_auto_draft": False,
                "requires_human": True,
                "applied_rule_ids": applied_rule_ids,
            }
        if confidence >= self.config.auto_draft_confidence_threshold:
            return {
                "action": "create_draft",
                "reason": f"Evidence is {verdict} with sufficient confidence",
                "allow_auto_draft": True,
                "requires_human": False,
                "applied_rule_ids": applied_rule_ids,
            }
        return {
            "action": "draft_for_review",
            "reason": "Moderate confidence; create draft but request human review",
            "allow_auto_draft": True,
            "requires_human": True,
            "applied_rule_ids": applied_rule_ids,
        }

    def review_reply_draft(
        self,
        case_type: str,
        claim_summary: str,
        evidence_summary: dict[str, Any],
        decision: dict[str, Any],
        draft_body: str,
        project: str | None = None,
        detected_language: str | None = None,
        matched_rule_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Review a draft for generic support safety before Gmail creation."""

        text = draft_body.casefold()
        issues: list[str] = []
        required_fixes: list[str] = []

        forbidden_patterns = [
            ("已发送", ["已发送", "已经发送", "sent this email", "has been sent"]),
            ("已到账", ["已到账", "已经到账", "credited", "delivered to your account"]),
            ("退款完成承诺", ["已退款", "已经退款", "refund has been processed", "refunded"]),
            ("补偿完成承诺", ["已补偿", "已经补偿", "compensation has been issued"]),
            ("问题已解决", ["问题已解决", "已经解决", "解决了这个问题", "resolved this issue", "issue has been fixed"]),
            ("未来版本承诺", ["下个版本一定", "保证上线", "will be released", "guarantee"]),
        ]
        for label, patterns in forbidden_patterns:
            if any(pattern.casefold() in text for pattern in patterns):
                issues.append(f"草稿包含{label}。")

        verdict = str(evidence_summary.get("verdict") or evidence_summary.get("status") or "")
        if verdict in {"inconclusive", "unavailable", "contradicted"}:
            certainty_patterns = ["核查到", "确认", "确定", "we confirmed", "we found that"]
            if any(pattern in text for pattern in certainty_patterns):
                issues.append("证据不足或矛盾时草稿包含确定性结论。")

        missing_fields = _filter_disallowed_missing_fields(
            [
                str(field)
                for field in decision.get("missing_fields", []) or []
                if str(field).strip()
            ]
        )
        missing_field_mentions = [
            field
            for field in missing_fields
            if not _draft_mentions_field(draft_body, field)
        ]
        if missing_field_mentions:
            required_fixes.append(
                "缺少必要字段询问: " + ", ".join(missing_field_mentions)
            )

        if _draft_asks_for_disallowed_identity(draft_body):
            issues.append(
                "草稿不应询问 server ID 或角色名；当前游戏无多服体系，仅需 user id。"
            )

        no_compensation_rules = {
            "crash_coin_loss_no_compensation",
            "crash_item_loss_details_request",
            "lost_item_clarify",
        }
        if any(rule_id in no_compensation_rules for rule_id in (matched_rule_ids or [])):
            compensation_promise_patterns = [
                "possible compensation",
                "look into possible compensation",
                "work toward a resolution",
                "we'll be able to check your account",
                "compensation for",
                "为您补偿",
                "补发道具",
                "补发金币",
                "道具补偿",
            ]
            if any(pattern in text for pattern in compensation_promise_patterns):
                issues.append("草稿不应暗示道具或金币补偿核查。")

        high_risk = case_type in self.config.high_risk_case_types
        if high_risk and decision.get("requires_human") and any(
            phrase in text
            for phrase in ["我们会为您处理", "we will process", "we have handled"]
        ):
            issues.append("高风险 case 转人工时草稿不应承诺已直接处理。")

        ok = not issues and not required_fixes
        return {
            "project": project,
            "case_type": case_type,
            "detected_language": detected_language,
            "claim_summary": claim_summary,
            "matched_rule_ids": matched_rule_ids or [],
            "ok": ok,
            "safe_to_create_draft": ok,
            "risk_level": "low" if ok else "high" if high_risk else "medium",
            "issues": issues,
            "required_fixes": required_fixes,
            "missing_field_mentions": missing_field_mentions,
        }


def _draft_mentions_field(draft_body: str, field: str) -> bool:
    text = draft_body.casefold()
    field_key = field.casefold()
    aliases = {
        "player_id": ["player id", "user id", "userid", "用户id", "用户 id"],
        "user_id": ["player id", "user id", "userid", "用户id", "用户 id"],
        "server_id": ["server id", "服务器", "区服"],
        "order_id": ["order id", "order number", "订单号", "订单编号", "订单"],
        "receipt": ["receipt", "收据", "凭证"],
        "screenshot": ["screenshot", "截图", "截屏"],
        "time": ["time", "时间", "发生时间"],
        "happened_at": ["time", "时间", "发生时间"],
        "device": ["device", "设备", "机型"],
        "app_version": ["version", "版本"],
    }
    candidates = aliases.get(field_key, [field_key.replace("_", " ")])
    return any(candidate in text for candidate in candidates)
