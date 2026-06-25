from player_support_agent.tools.config import SupportPolicyConfig
from player_support_agent.tools.decision_tools import DecisionTools


def test_extract_feedback_claim_recommends_global_no_content_label_for_any_project():
    decisions = DecisionTools(
        SupportPolicyConfig(
            label_by_case_type={
                "no_content": ["无内容"],
            }
        )
    )

    numbercrush = decisions.extract_feedback_claim(
        project="NumberCrush",
        case_type="no_content",
        summary="Only platform metadata and empty question field",
    )
    blackhole = decisions.extract_feedback_claim(
        project="BlackHole",
        case_type="no_content",
        summary="Only gibberish without a describable issue",
    )

    assert numbercrush["recommended_labels"] == ["无内容"]
    assert blackhole["recommended_labels"] == ["无内容"]


def test_extract_feedback_claim_bug_recommends_bug_feedback_label():
    decisions = DecisionTools(
        SupportPolicyConfig(
            label_suffix_by_case_type={
                "bug": ["bug反馈"],
            }
        )
    )

    result = decisions.extract_feedback_claim(
        project="BlackHole",
        case_type="bug",
        summary="Treasure Tide UI shows blank space blocking free reward",
        available_label_names=["BlackHole", "BlackHole/bug反馈"],
    )

    assert result["recommended_labels"] == ["BlackHole", "BlackHole/bug反馈"]


def test_extract_feedback_claim_ad_promo_mismatch_recommends_ad_issue_label():
    decisions = DecisionTools(
        SupportPolicyConfig(
            label_suffix_by_case_type={
                "ad_promo_mismatch": ["广告问题", "广告"],
            }
        )
    )

    result = decisions.extract_feedback_claim(
        project="BlackHole",
        case_type="ad_promo_mismatch",
        summary="Promo promised 2 days off ads but game still shows many ads",
        available_label_names=[
            "BlackHole",
            "BlackHole/广告问题",
            "BlackHole/广告",
        ],
    )

    assert result["recommended_labels"] == ["BlackHole", "BlackHole/广告问题", "BlackHole/广告"]


def test_decide_support_action_ad_promo_mismatch_requires_apply_label_only_rule():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.decide_support_action(
        case_type="ad_promo_mismatch",
        verdict="supported",
        confidence=0.9,
        risk_level="low",
        applied_rule_ids=["ad_promo_mismatch_label_only"],
        rule_action="apply_label_only",
        rule_human_review=False,
    )

    assert result["action"] == "skip_label_only"
    assert result["allow_auto_draft"] is False
    assert result["requires_human"] is False
    assert "label only" in result["reason"].casefold()


def test_decide_support_action_ad_promo_mismatch_case_type_alone_does_not_skip():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.decide_support_action(
        case_type="ad_promo_mismatch",
        verdict="supported",
        confidence=0.9,
        risk_level="low",
        applied_rule_ids=[],
        rule_action=None,
        rule_human_review=False,
    )

    assert result["action"] != "skip_label_only"


def test_decide_support_action_no_content_case_type_alone_does_not_skip():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.decide_support_action(
        case_type="no_content",
        verdict="inconclusive",
        confidence=0.0,
        risk_level="low",
        missing_fields=["player_id"],
        applied_rule_ids=[],
        rule_action=None,
        rule_human_review=False,
    )

    assert result["action"] != "skip_label_only"


def test_decide_support_action_evidence_action_requires_applied_rule_ids():
    decisions = DecisionTools(SupportPolicyConfig())

    without_rules = decisions.decide_support_action(
        case_type="ads_after_purchase",
        verdict="inconclusive",
        confidence=0.6,
        risk_level="low",
        evidence_recommended_action="draft_for_review",
        applied_rule_ids=[],
        rule_action=None,
        rule_human_review=False,
    )
    with_rules = decisions.decide_support_action(
        case_type="ads_after_purchase",
        verdict="inconclusive",
        confidence=0.6,
        risk_level="low",
        evidence_recommended_action="draft_for_review",
        applied_rule_ids=["remove_ads_no_order_request_order_id"],
        rule_action="draft_reply",
        rule_human_review=False,
    )

    assert without_rules["action"] != "draft_for_review"
    assert with_rules["action"] == "draft_for_review"


def test_decide_support_action_apply_label_only_skips_draft_even_with_missing_fields():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.decide_support_action(
        case_type="no_content",
        verdict="inconclusive",
        confidence=0.0,
        risk_level="low",
        missing_fields=["player_id", "server_id"],
        applied_rule_ids=["empty_feedback_apply_no_content_label"],
        rule_action="apply_label_only",
        rule_human_review=False,
    )

    assert result["action"] == "skip_label_only"
    assert result["allow_auto_draft"] is False
    assert result["requires_human"] is False


def test_extract_feedback_claim_filters_recommended_labels_by_project():
    decisions = DecisionTools(
        SupportPolicyConfig(
            label_by_case_type={
                "payment": ["NumberCrush", "NumberCrush/内购问题"],
            }
        )
    )

    numbercrush = decisions.extract_feedback_claim(
        project="NumberCrush",
        case_type="payment",
        summary="Payment issue",
    )
    blackhole = decisions.extract_feedback_claim(
        project="BlackHole",
        case_type="payment",
        summary="Payment issue",
    )

    assert numbercrush["recommended_labels"] == ["NumberCrush", "NumberCrush/内购问题"]
    assert blackhole["recommended_labels"] == []


def test_extract_feedback_claim_ads_after_purchase_prefers_remove_ads_label():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.extract_feedback_claim(
        project="NumberCrush",
        case_type="ads_after_purchase",
        summary="Still seeing ads after remove-ads purchase",
        available_label_names=[
            "NumberCrush",
            "NumberCrush/去广告后有广告",
            "NumberCrush/内购问题",
        ],
    )

    assert result["recommended_labels"] == [
        "NumberCrush",
        "NumberCrush/去广告后有广告",
    ]
    assert result["label_selection_reason"] == "primary_suffix"


def test_extract_feedback_claim_ads_after_purchase_falls_back_to_payment_label():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.extract_feedback_claim(
        project="Number Sum",
        case_type="ads_after_purchase",
        summary="Still seeing ads after remove-ads purchase",
        available_label_names=[
            "Number Sum",
            "Number Sum/内购问题",
            "Number Sum/广告问题",
        ],
    )

    assert result["recommended_labels"] == ["Number Sum", "Number Sum/内购问题"]
    assert result["label_selection_reason"] == "fallback_payment_suffix"


def test_decide_support_action_maps_remove_ads_order_request_alias():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.decide_support_action(
        case_type="ads_after_purchase",
        verdict="inconclusive",
        confidence=0.6,
        risk_level="low",
        missing_fields=["order_id or purchase receipt"],
        evidence_recommended_action="ask_for_order_id_or_receipt",
        applied_rule_ids=["remove_ads_no_order_request_order_id"],
    )

    assert result["action"] == "draft_for_review"
    assert result["requires_human"] is True


def test_extract_feedback_claim_builds_project_suffix_label_candidates():
    decisions = DecisionTools(
        SupportPolicyConfig(
            label_suffix_by_case_type={
                "payment": ["内购问题", "内购"],
            }
        )
    )

    result = decisions.extract_feedback_claim(
        project="Bus Jam Master",
        case_type="payment",
        summary="Payment issue",
        available_label_names=["Bus Jam Master", "Bus Jam Master/内购"],
    )

    assert result["recommended_labels"] == ["Bus Jam Master", "Bus Jam Master/内购"]


def test_extract_feedback_claim_preserves_detected_language_context():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.extract_feedback_claim(
        project="Number Sum",
        case_type="bug",
        summary="Daily challenge is a gray square",
        detected_language="English",
        language_source_text=(
            "Daily challenge is messed up, has been coming across as one big "
            "gray square last two days, no puzzle"
        ),
    )

    assert result["detected_language"] == "English"
    assert result["language_source_text"].startswith("Daily challenge")


def test_specific_rule_can_allow_supported_payment_draft():
    decisions = DecisionTools(
        SupportPolicyConfig(
            high_risk_case_types=["payment"],
            auto_draft_confidence_threshold=0.85,
        )
    )

    result = decisions.decide_support_action(
        case_type="payment",
        verdict="supported",
        confidence=0.92,
        risk_level="low",
        applied_rule_ids=["payment_pass_not_instant_delivery"],
        rule_action="draft_reply",
        rule_human_review=False,
    )

    assert result["action"] == "create_draft"
    assert result["requires_human"] is False
    assert result["applied_rule_ids"] == ["payment_pass_not_instant_delivery"]


def test_specific_rule_does_not_override_high_risk_assessment():
    decisions = DecisionTools(
        SupportPolicyConfig(
            high_risk_case_types=["payment"],
            auto_draft_confidence_threshold=0.85,
        )
    )

    result = decisions.decide_support_action(
        case_type="payment",
        verdict="supported",
        confidence=0.92,
        risk_level="high",
        applied_rule_ids=["payment_pass_not_instant_delivery"],
        rule_action="draft_reply",
        rule_human_review=False,
    )

    assert result["action"] == "handoff_human"
    assert result["requires_human"] is True


def test_review_reply_draft_rejects_sent_or_refund_claims():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.review_reply_draft(
        project="NumberCrush",
        case_type="payment",
        detected_language="zh-CN",
        claim_summary="玩家说购买未到账",
        matched_rule_ids=["payment_issue_handoff"],
        evidence_summary={"verdict": "inconclusive"},
        decision={"action": "handoff_human", "missing_fields": []},
        draft_body="您好，我们已经退款并解决了这个问题。",
    )

    assert result["ok"] is False
    assert result["safe_to_create_draft"] is False
    assert any("退款" in issue for issue in result["issues"])
    assert any("问题已解决" in issue for issue in result["issues"])


def test_resolve_player_identity_does_not_require_server_id():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.resolve_player_identity(
        player_id="e5bc9d52b4d5ec72",
        email="karissamicheletti@gmail.com",
    )

    assert result["resolved"] is True
    assert result["missing_fields"] == []


def test_assess_coin_frenzy_log_evidence_coin_frenzy_branch():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.assess_coin_frenzy_log_evidence(
        purchase_success_found=True,
        purchase_product_id="coin.frenzy",
    )

    assert result["branch_id"] == "coin_frenzy_purchase_confirmed"
    assert result["recommended_template_id"] == "coin_frenzy_task_reward_explanation"
    assert result["recommended_action"] == "create_draft"


def test_decide_support_action_normalizes_feature_request_rule_alias():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.decide_support_action(
        case_type="feature_request",
        verdict="supported",
        confidence=0.9,
        risk_level="low",
        applied_rule_ids=["feature_request_general_acknowledge"],
        rule_action="draft_reply",
        rule_human_review=False,
    )

    assert result["action"] == "create_draft"
    assert result["applied_rule_ids"] == ["feature_request_ack"]


def test_review_reply_draft_works_without_detected_language():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.review_reply_draft(
        project="BlackHole",
        case_type="feature_request",
        claim_summary="Player wants timer eased",
        matched_rule_ids=["feature_request_ack"],
        evidence_summary={"verdict": "supported"},
        decision={"action": "create_draft", "missing_fields": []},
        draft_body="Hello, thank you for your feedback.",
    )

    assert result["ok"] is True
    assert result["detected_language"] is None


def test_decide_support_action_honors_coin_frenzy_evidence_recommendation():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.decide_support_action(
        case_type="pass_purchase_misunderstanding",
        verdict="supported",
        confidence=0.9,
        risk_level="low",
        evidence_recommended_action="create_draft",
        applied_rule_ids=["coin_frenzy_activity_log_investigation"],
        rule_action="draft_reply",
        rule_human_review=False,
    )

    assert result["action"] == "create_draft"


def test_assess_remove_ads_log_evidence_rv_only_branch():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.assess_remove_ads_log_evidence(
        purchase_success_found=True,
        remove_ads_product_id="removead_pack",
        remove_ads_purchase_time="2026-06-10 12:00:00",
        latest_interstitial_time="2026-06-09 10:00:00",
    )

    assert result["branch_id"] == "rv_only_after_purchase"
    assert result["recommended_template_id"] == "ads_after_purchase_rv_only_explanation"
    assert result["recommended_action"] == "create_draft"


def test_assess_remove_ads_log_evidence_no_purchase_branch():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.assess_remove_ads_log_evidence(
        purchase_success_found=False,
    )

    assert result["branch_id"] == "no_purchase_success"
    assert result["recommended_template_id"] == "remove_ads_no_order_request_order_id"
    assert result["recommended_action"] == "draft_for_review"


def test_assess_remove_ads_log_evidence_forced_interstitial_handoff():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.assess_remove_ads_log_evidence(
        purchase_success_found=True,
        remove_ads_product_id="remove_ads",
        remove_ads_purchase_time="2026-06-10 12:00:00",
        latest_interstitial_time="2026-06-11 08:00:00",
    )

    assert result["branch_id"] == "forced_interstitial_after_purchase"
    assert result["recommended_action"] == "handoff_human"


def test_decide_support_action_honors_remove_ads_evidence_recommendation():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.decide_support_action(
        case_type="ads_after_purchase",
        verdict="supported",
        confidence=0.9,
        risk_level="low",
        evidence_recommended_action="create_draft",
        applied_rule_ids=["ads_after_purchase_log_investigation"],
        rule_action="draft_reply",
        rule_human_review=False,
    )

    assert result["action"] == "create_draft"


def test_decide_support_action_allows_ad_issue_rule_draft_without_logs():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.decide_support_action(
        case_type="ad_issue",
        verdict="inconclusive",
        confidence=0.6,
        risk_level="low",
        applied_rule_ids=["ad_loading_playback_troubleshooting"],
        rule_action="draft_reply",
        rule_human_review=False,
    )

    assert result["action"] == "create_draft"
    assert result["requires_human"] is False


def test_decide_support_action_ignores_optional_identity_fields():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.decide_support_action(
        case_type="crash_or_freeze",
        verdict="inconclusive",
        confidence=0.6,
        risk_level="low",
        missing_fields=["server_id", "character_name"],
        applied_rule_ids=["crash_coin_loss_no_compensation"],
        rule_action="draft_reply",
        rule_human_review=False,
    )

    assert result["action"] != "draft_missing_info"
    assert result["requires_human"] is True


def test_extract_feedback_claim_strips_server_identity_missing_fields():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.extract_feedback_claim(
        case_type="bug",
        summary="Lag issue",
        missing_fields=["server_id", "character_name", "screenshot"],
    )

    assert result["missing_fields"] == ["screenshot"]


def test_review_reply_draft_rejects_server_id_for_any_case():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.review_reply_draft(
        project="BlackHole",
        case_type="crash_or_freeze",
        detected_language="English",
        claim_summary="Paid coins to continue and got kicked out",
        matched_rule_ids=["crash_coin_loss_no_compensation"],
        evidence_summary={"verdict": "inconclusive"},
        decision={"action": "create_draft", "missing_fields": []},
        draft_body=(
            "Hi there, please share your server ID and character name so we can "
            "look into possible compensation."
        ),
    )

    assert result["ok"] is False
    assert result["safe_to_create_draft"] is False
    assert any("server ID" in issue for issue in result["issues"])
    assert any("补偿" in issue for issue in result["issues"])


def test_review_reply_draft_rejects_server_id_for_lag_case():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.review_reply_draft(
        project="Number Sum",
        case_type="bug",
        detected_language="English",
        claim_summary="Blank page freeze",
        matched_rule_ids=["lag_details_request"],
        evidence_summary={"verdict": "inconclusive"},
        decision={"action": "create_draft", "missing_fields": []},
        draft_body=(
            "Please provide your server ID (you can usually find this in the game's "
            "settings menu) and your character name in the game."
        ),
    )

    assert result["ok"] is False
    assert any("server ID" in issue for issue in result["issues"])


def test_review_reply_draft_accepts_crash_coin_loss_template():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.review_reply_draft(
        project="BlackHole",
        case_type="crash_or_freeze",
        detected_language="English",
        claim_summary="Paid coins to continue and got kicked out",
        matched_rule_ids=["crash_coin_loss_no_compensation"],
        evidence_summary={"verdict": "inconclusive"},
        decision={"action": "create_draft", "missing_fields": []},
        draft_body=(
            "Hi there, we're sorry you were kicked out after spending coins to continue. "
            "Does this crash happen frequently? We currently do not have the ability to "
            "restore or compensate in-game coins when a crash happens."
        ),
    )

    assert result["ok"] is True
    assert result["safe_to_create_draft"] is True


def test_review_reply_draft_requires_missing_fields():
    decisions = DecisionTools(SupportPolicyConfig())

    result = decisions.review_reply_draft(
        project="NumberCrush",
        case_type="payment",
        detected_language="zh-CN",
        claim_summary="玩家说购买未到账",
        matched_rule_ids=[],
        evidence_summary={"verdict": "inconclusive"},
        decision={"action": "draft_missing_info", "missing_fields": ["order_id", "screenshot"]},
        draft_body="您好，请提供您的 user id，我们会继续核查。",
    )

    assert result["ok"] is False
    assert "order_id" in result["missing_field_mentions"]
    assert "screenshot" in result["missing_field_mentions"]
    assert any("缺少必要字段询问" in fix for fix in result["required_fixes"])
