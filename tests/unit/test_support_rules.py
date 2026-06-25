from pathlib import Path

from player_support_agent.tools.config import KnowledgeConfig, SupportAgentConfig
from player_support_agent.tools.forge_tools import build_tool_defs
from player_support_agent.tools.rule_tools import RuleTools


def test_relevant_support_rules_match_no_content_case_type(tmp_path: Path):
    rules_path = tmp_path / "rules.toml"
    rules_path.write_text(
        """
[[rules]]
id = "empty_feedback_apply_no_content_label"
case_types = ["no_content"]
always_include = true
priority = 100
summary = "Apply 无内容 and skip reply."
action = "apply_label_only"
instructions = ["No substantive feedback."]
""".strip(),
        encoding="utf-8",
    )
    tools = RuleTools(
        KnowledgeConfig(rules_path=str(rules_path), templates_dir=str(tmp_path)),
    )

    result = tools.get_relevant_support_rules(
        case_type="no_content",
        email_text=(
            "platform:Android ver:1.0.0 userid:d45cc3256e908b14\\n"
            "I need some help. My question is:"
        ),
        project="BlackHole",
    )

    assert result["matched_rules"][0]["id"] == "empty_feedback_apply_no_content_label"
    assert result["matched_rules"][0]["action"] == "apply_label_only"


def test_relevant_support_rules_match_case_type_and_triggers(tmp_path: Path):
    rules_path = tmp_path / "rules.toml"
    rules_path.write_text(
        """
[[rules]]
id = "payment_pass_not_instant_delivery"
case_types = ["payment"]
triggers = ["没到账", "pass"]
priority = 100
summary = "Pass rewards unlock by stage."
requires_logs = true
required_evidence = ["product_id"]
reply_template = "payment_pass_explanation"
instructions = ["Check product_id first."]
""".strip(),
        encoding="utf-8",
    )
    tools = RuleTools(
        KnowledgeConfig(rules_path=str(rules_path), templates_dir=str(tmp_path)),
    )

    result = tools.get_relevant_support_rules(
        case_type="payment",
        email_text="我买了 pass 但是奖励没到账",
    )

    assert result["matched_rules"][0]["id"] == "payment_pass_not_instant_delivery"
    assert result["matched_rules"][0]["requires_logs"] is True
    assert set(result["matched_rules"][0]["trigger_hits"]) == {"没到账", "pass"}


def test_get_reply_template_uses_default_language(tmp_path: Path):
    template_dir = tmp_path / "zh-CN"
    template_dir.mkdir()
    template_path = template_dir / "payment_pass_explanation.md"
    template_path.write_text("Pass rewards unlock by stage.", encoding="utf-8")
    tools = RuleTools(
        KnowledgeConfig(rules_path=str(tmp_path / "missing.toml"), templates_dir=str(tmp_path)),
        default_language="zh-CN",
    )

    result = tools.get_reply_template("payment_pass_explanation")

    assert result["language"] == "zh-CN"
    assert result["body"] == "Pass rewards unlock by stage."


def test_support_knowledge_summary_counts_rules_and_templates(tmp_path: Path):
    rules_path = tmp_path / "rules.toml"
    rules_path.write_text(
        """
[[rules]]
id = "rule_one"
summary = "Rule one."
always_include = true
""".strip(),
        encoding="utf-8",
    )
    template_dir = tmp_path / "templates" / "zh-CN"
    template_dir.mkdir(parents=True)
    (template_dir / "rule_one.md").write_text("Template.", encoding="utf-8")
    tools = RuleTools(
        KnowledgeConfig(
            rules_path=str(rules_path),
            templates_dir=str(tmp_path / "templates"),
        ),
    )

    result = tools.get_support_knowledge_summary()

    assert result["rule_count"] == 1
    assert result["rule_ids"] == ["rule_one"]
    assert result["template_count"] == 1
    assert result["template_languages"] == ["zh-CN"]


def test_project_specific_rules_path_is_selected(tmp_path: Path):
    default_rules = tmp_path / "default.toml"
    blackhole_rules = tmp_path / "blackhole.toml"
    default_rules.write_text(
        """
[[rules]]
id = "default_rule"
summary = "Default."
always_include = true
""".strip(),
        encoding="utf-8",
    )
    blackhole_rules.write_text(
        """
[[rules]]
id = "blackhole_rule"
projects = ["BlackHole"]
case_types = ["payment"]
triggers = ["pass"]
summary = "BlackHole pass rule."
""".strip(),
        encoding="utf-8",
    )
    tools = RuleTools(
        KnowledgeConfig(
            rules_path=str(default_rules),
            templates_dir=str(tmp_path / "templates"),
            project_rules_paths={"BlackHole": str(blackhole_rules)},
        )
    )

    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="payment",
        email_text="I bought a pass",
    )

    assert result["project"] == "BlackHole"
    assert result["matched_rules"][0]["id"] == "blackhole_rule"
    assert {rule["id"] for rule in result["matched_rules"]} == {
        "blackhole_rule",
        "default_rule",
    }


def test_support_coverage_summary_reports_missing_profiles_rules_templates(tmp_path: Path):
    default_rules = tmp_path / "default.toml"
    default_rules.write_text(
        """
[[rules]]
id = "default_rule"
summary = "Default."
always_include = true
""".strip(),
        encoding="utf-8",
    )
    blackhole_rules = tmp_path / "blackhole.toml"
    blackhole_rules.write_text(
        """
[[rules]]
id = "blackhole_rule"
projects = ["BlackHole"]
summary = "BlackHole."
always_include = true
""".strip(),
        encoding="utf-8",
    )
    blackhole_profile = tmp_path / "blackhole_profile.toml"
    blackhole_profile.write_text('project = "BlackHole"\n', encoding="utf-8")

    tools = RuleTools(
        KnowledgeConfig(
            rules_path=str(default_rules),
            templates_dir=str(tmp_path / "templates"),
            project_rules_paths={"BlackHole": str(blackhole_rules)},
            project_profiles_paths={"BlackHole": str(blackhole_profile)},
        ),
        project_label_names=["BlackHole", "Water Sort"],
        clickhouse_project_case_type_tables={"BlackHole": {"*": ["blackhole"]}},
        label_suffix_by_case_type={"payment": ["内购问题"]},
    )

    result = tools.get_support_coverage_summary()

    by_project = {item["project"]: item for item in result["projects"]}
    assert by_project["BlackHole"]["has_clickhouse_mapping"] is True
    assert by_project["BlackHole"]["has_project_rules"] is True
    assert by_project["BlackHole"]["has_project_profile"] is True
    assert by_project["Water Sort"]["has_clickhouse_mapping"] is False
    assert by_project["Water Sort"]["uses_generic_rules_only"] is True
    assert "Water Sort" in result["projects_missing_clickhouse_mapping"]


def test_get_project_support_profile_returns_found_profile(tmp_path: Path):
    profile_path = tmp_path / "blackhole.toml"
    profile_path.write_text(
        """
project = "BlackHole"
aliases = ["Black Hole"]

[policy]
supports_cloud_save = false
""".strip(),
        encoding="utf-8",
    )
    tools = RuleTools(
        KnowledgeConfig(project_profiles_paths={"BlackHole": str(profile_path)}),
        project_label_names=["BlackHole"],
        clickhouse_project_case_type_tables={"BlackHole": {"*": ["blackhole"]}},
    )

    result = tools.get_project_support_profile("BlackHole")

    assert result["profile_found"] is True
    assert result["profile"]["project"] == "BlackHole"
    assert result["profile"]["policy"]["supports_cloud_save"] is False


def test_get_project_support_profile_missing_profile_fails_soft(tmp_path: Path):
    tools = RuleTools(
        KnowledgeConfig(templates_dir=str(tmp_path / "templates")),
        project_label_names=["Water Sort"],
        clickhouse_project_case_type_tables={},
        label_suffix_by_case_type={"payment": ["内购问题"]},
    )

    result = tools.get_project_support_profile("Water Sort")

    assert result["profile_found"] is False
    assert result["project"] == "Water Sort"
    assert result["safe_summary"]["has_clickhouse_mapping"] is False
    assert result["safe_summary"]["label_suffix_by_case_type"]["payment"] == ["内购问题"]


def test_relevant_support_rules_always_include_no_server_identity_rule():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="bug",
        email_text="The game freezes on level 5.",
    )

    assert "never_ask_server_or_character_name" in {
        rule["id"] for rule in result["matched_rules"]
    }


def test_relevant_support_rules_include_case_type_defaults_without_triggers():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="ad_issue",
        email_text="The adds keep freezing & I need to keep getting out of the game",
        include_case_defaults=True,
    )

    matched_ids = {rule["id"] for rule in result["matched_rules"]}
    assert "ad_issue_screenshot_request" in matched_ids
    assert "never_ask_server_or_character_name" in matched_ids


def test_relevant_support_rules_match_maiken_ad_promo_mismatch_email():
    tools = RuleTools(KnowledgeConfig())

    email_text = (
        "platform:Android ver:1.16.3 userid:aa6a13e428c67772\n"
        "I need some help. My question is: New to the game, strated today. "
        "I am supposed to get 2 days off of ads, but here are MANY ads. "
        "All the time. What gives?"
    )
    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="ad_promo_mismatch",
        email_text=email_text,
        include_case_defaults=True,
    )

    promo_rule = next(
        rule
        for rule in result["matched_rules"]
        if rule["id"] == "ad_promo_mismatch_label_only"
    )
    assert promo_rule["action"] == "apply_label_only"
    assert promo_rule["requires_logs"] is False
    assert "supposed to get" in promo_rule["trigger_hits"]
    assert "what gives" in promo_rule["trigger_hits"]


def test_relevant_support_rules_match_patricia_coin_frenzy_email():
    tools = RuleTools(KnowledgeConfig())

    email_text = (
        "I purchase the beginning pack didn't get my credit, also playing this game "
        "black on level 208 haven't received my rewards for completing the level "
        "requirement."
    )
    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="pass_purchase_misunderstanding",
        email_text=email_text,
        include_case_defaults=True,
    )

    assert result["matched_rules"][0]["id"] == "coin_frenzy_activity_log_investigation"
    assert result["matched_rules"][0]["reply_template"] == "coin_frenzy_task_reward_explanation"
    assert "beginning pack" in result["matched_rules"][0]["trigger_hits"]


def test_relevant_support_rules_match_treasure_tide_blank_page_email():
    tools = RuleTools(KnowledgeConfig())

    email_text = (
        "I paid for a few of the \"Treasure Tide\" rewards and well after I paid for "
        "the 2nd or 3rd time it shows a blank space and then the \"free\" reward "
        "however, I can't access the \"free\" rewards because of that blank space."
    )
    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="bug",
        email_text=email_text,
        include_case_defaults=True,
    )

    assert result["matched_rules"][0]["id"] == "blank_page_screenshot_request"
    assert result["matched_rules"][0]["reply_template"] == "blank_page_screenshot_request"
    assert "blank space" in result["matched_rules"][0]["trigger_hits"]
    matched_ids = {rule["id"] for rule in result["matched_rules"]}
    assert "payment_issue_handoff" not in matched_ids


def test_payment_issue_handoff_requires_explicit_non_delivery():
    tools = RuleTools(KnowledgeConfig())

    paid_only = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="payment",
        email_text="I paid for Treasure Tide rewards but there is a blank space.",
        include_case_defaults=True,
    )
    not_received = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="payment",
        email_text="I paid but did not receive the coins in my account.",
        include_case_defaults=True,
    )

    assert paid_only["matched_rules"][0]["id"] == "blank_page_screenshot_request"
    assert "payment_issue_handoff" not in {
        rule["id"] for rule in paid_only["matched_rules"]
    }
    handoff = next(
        rule
        for rule in not_received["matched_rules"]
        if rule["id"] == "payment_issue_handoff"
    )
    assert "did not receive" in handoff["trigger_hits"]


def test_get_reply_template_loads_coin_frenzy_task_reward_explanation_en():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_reply_template(
        "coin_frenzy_task_reward_explanation",
        language="English",
    )

    assert result["language"] == "en"
    assert "activity tasks" in result["body"].casefold()


def test_get_reply_template_loads_blank_page_screenshot_request_en():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_reply_template(
        "blank_page_screenshot_request",
        language="English",
    )

    assert result["language"] == "en"
    assert "screenshot" in result["body"].casefold()


def test_relevant_support_rules_match_ad_redirect_shein_email():
    tools = RuleTools(KnowledgeConfig())

    email_text = (
        "My phone migrates to SHEIN throughout any game. "
        "Often opening 10 separate pages. Very annoying"
    )
    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="ad_issue",
        email_text=email_text,
    )

    assert result["matched_rules"][0]["id"] == "ad_redirect_reset_ad_id"
    assert result["matched_rules"][0]["reply_template"] == "ad_redirect_reset_ad_id"
    assert "migrates" in result["matched_rules"][0]["trigger_hits"]


def test_get_reply_template_loads_ad_redirect_reset_ad_id_en():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_reply_template(
        "ad_redirect_reset_ad_id",
        language="English",
    )

    assert result["language"] == "en"
    assert "Reset advertising ID" in result["body"] or "advertising ID" in result["body"]
    assert "storage" not in result["body"].casefold()


def test_relevant_support_rules_match_ad_issue_freezing_email():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="ad_issue",
        email_text="The adds keep freezing & I need to keep getting out of the game",
    )

    ad_rule = next(
        rule
        for rule in result["matched_rules"]
        if rule["id"] == "ad_loading_playback_troubleshooting"
    )
    assert ad_rule["reply_template"] == "ad_loading_playback_troubleshooting"
    assert ad_rule["requires_logs"] is False
    assert "freezing" in ad_rule["trigger_hits"]


def test_relevant_support_rules_match_feature_request_timer_email():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="feature_request",
        email_text=(
            "platform:iOS ver:1.16.3 userid:4faa31c6a8454fe\n"
            "I need some help. My question is: ease up with the timer"
        ),
    )

    assert result["matched_rules"][0]["id"] == "feature_request_ack"
    assert "ease up" in result["matched_rules"][0]["trigger_hits"]
    assert result["has_strong_match"] is True
    assert result["recommended_rule_id"] == "feature_request_ack"
    assert result["guidance"] is not None
    assert "requires_logs=false" in result["guidance"]
    assert "get_coin_frenzy_investigation_playbook" in result["guidance"]


def test_relevant_support_rules_timer_email_does_not_weak_match_coin_frenzy():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="gameplay_misunderstanding",
        email_text="ease up with the timer",
        include_case_defaults=True,
    )

    matched_ids = {rule["id"] for rule in result["matched_rules"]}
    assert "coin_frenzy_activity_log_investigation" not in matched_ids


def test_relevant_support_rules_timer_email_does_not_match_crash_coin_loss():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="gameplay_misunderstanding",
        email_text="ease up with the timer",
        include_case_defaults=True,
    )

    matched_ids = {rule["id"] for rule in result["matched_rules"]}
    assert "crash_coin_loss_no_compensation" not in matched_ids


def test_relevant_support_rules_coin_frenzy_matches_level_up_credit_email():
    tools = RuleTools(KnowledgeConfig())

    email_text = (
        "why am I not receiving credit for my level ups when im still on "
        "the time restraint and it hasn't expired yet?"
    )
    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="pass_purchase_misunderstanding",
        email_text=email_text,
    )

    coin_rule = next(
        rule
        for rule in result["matched_rules"]
        if rule["id"] == "coin_frenzy_activity_log_investigation"
    )
    assert "level ups" in coin_rule["trigger_hits"]
    assert "time restraint" in coin_rule["trigger_hits"]
    assert result["has_strong_match"] is True


def test_relevant_support_rules_deprioritizes_policy_only_rule_for_feature_request():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="feature_request",
        email_text="ease up with the timer",
    )

    assert result["matched_rules"][0]["id"] == "feature_request_ack"
    assert result["recommended_rule_id"] == "feature_request_ack"


def test_get_reply_template_loads_feature_request_ack_en():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_reply_template(
        "feature_request_ack",
        language="English",
    )

    assert result["language"] == "en"
    assert result["requested_language"] == "en"
    assert result["language_fallback"] is False
    assert "product and development teams" in result["body"]


def test_get_reply_template_reports_language_fallback_for_missing_language(tmp_path: Path):
    template_dir = tmp_path / "zh-CN"
    template_dir.mkdir()
    template_path = template_dir / "fallback_only_template.md"
    template_path.write_text("中文模板", encoding="utf-8")
    tools = RuleTools(
        KnowledgeConfig(
            rules_path=str(tmp_path / "missing.toml"),
            templates_dir=str(tmp_path),
        ),
        default_language="zh-CN",
    )

    result = tools.get_reply_template(
        "fallback_only_template",
        language="fr",
    )

    assert result["language"] == "zh-CN"
    assert result["requested_language"] == "fr"
    assert result["language_fallback"] is True
    assert "detected_language" in result["guidance"]


def test_relevant_support_rules_match_crash_coin_loss_email():
    tools = RuleTools(KnowledgeConfig())

    email_text = (
        "platform:Android ver:1.16.3 userid:e5bc9d52b4d5ec72\n"
        "I need some help. My question is: at least three times I've paid coins "
        "I HAVE BOUGHT to continue playing a level and I got kicked out of the app."
    )
    result = tools.get_relevant_support_rules(
        project="BlackHole",
        case_type="crash_or_freeze",
        email_text=email_text,
    )

    assert result["matched_rules"][0]["id"] == "crash_coin_loss_no_compensation"
    assert result["matched_rules"][0]["reply_template"] == "crash_coin_loss_no_compensation"


def test_relevant_support_rules_match_ads_after_purchase_email():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_relevant_support_rules(
        project="Number Sum",
        case_type="ads_after_purchase",
        email_text="I bought remove ads but still see ads after every level.",
        include_case_defaults=True,
    )

    matched_ids = {rule["id"] for rule in result["matched_rules"]}
    assert "ads_after_purchase_log_investigation" in matched_ids


def test_get_reply_template_loads_ads_after_purchase_rv_only_template():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_reply_template("ads_after_purchase_rv_only_explanation")

    assert "插屏广告" in result["body"]
    assert "RV" in result["body"]


def test_legacy_reply_templates_match_blackhole_card_album():
    tools = RuleTools(KnowledgeConfig())

    result = tools.search_legacy_reply_templates(
        project="BlackHole",
        case_type="feature_request",
        email_text="When will the new card album event come?",
    )

    assert result["template_count"] >= 20
    assert result["matched_templates"]
    assert any(
        item.get("source") == "legacy_template"
        for item in result["matched_templates"]
    )


def test_get_relevant_support_rules_includes_legacy_templates():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_relevant_support_rules(
        project="BusFever",
        case_type="gameplay_misunderstanding",
        email_text="What are free passes and streak rewards?",
    )

    assert "matched_legacy_templates" in result
    assert isinstance(result["matched_legacy_templates"], list)


def test_get_reply_template_loads_legacy_blackhole_template():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_reply_template("blackhole_topic_0757875e09", project="BlackHole")

    assert result["template_id"] == "blackhole_topic_0757875e09"
    assert "指南针" in result["body"] or "物品" in result["body"]


def test_get_reply_template_maps_english_alias_to_en_template():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_reply_template(
        "ad_loading_playback_troubleshooting",
        language="English",
    )

    assert result["language"] == "en"
    assert "shuts down" in result["body"] or "sorry" in result["body"].casefold()


def test_relevant_support_rules_match_grill_master_ad_shutdown_email():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_relevant_support_rules(
        project="Grill Master",
        case_type="ad_issue",
        email_text=(
            "Your game keeps shutting down and wipes out my progress. "
            "Every time an ad comes up."
        ),
    )

    ad_rule = next(
        rule
        for rule in result["matched_rules"]
        if rule["id"] == "ad_loading_playback_troubleshooting"
    )
    assert "shutting down" in ad_rule["trigger_hits"]
    assert ad_rule["requires_logs"] is False


def test_get_reply_template_returns_english_crash_coin_loss_template():
    tools = RuleTools(KnowledgeConfig())

    result = tools.get_reply_template(
        "crash_coin_loss_no_compensation",
        language="en",
    )

    assert result["language"] == "en"
    assert "do not have the ability to restore or compensate" in result["body"]


def test_support_knowledge_summary_tool_registered():
    tool_defs = build_tool_defs(SupportAgentConfig())

    assert "get_support_knowledge_summary" in tool_defs
    assert "get_support_coverage_summary" in tool_defs
    assert "get_project_support_profile" in tool_defs
