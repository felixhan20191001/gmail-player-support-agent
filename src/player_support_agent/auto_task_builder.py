"""Build natural-language auto-processing tasks for the agent."""

from __future__ import annotations

from typing import Any

from .agent_runner import LIVE_CONFIRMATION
from .prompts import THREAD_CONVERSATION_REMINDER_ZH


def _format_task_list(value: Any) -> str:
    if not isinstance(value, list):
        return "[]"
    return str([str(item) for item in value if item is not None])


def build_auto_task(
    messages: list[dict[str, Any]],
    *,
    live_run: bool = False,
) -> str:
    """Build the only task payload the scheduler gives to the model."""

    mode = (
        f"本次自动处理已包含确认短语：{LIVE_CONFIRMATION}。允许创建 Gmail 草稿、"
        "应用已有标签、写处理状态，但仍然禁止发送邮件。"
        if live_run
        else "本次自动处理为 dry-run。请模拟 Gmail 写操作，但仍完整完成分析和最终总结。"
    )
    lines = [
        mode,
        "",
        "请处理以下新增玩家反馈邮件。调度器只完成了候选邮件发现和去重；",
        "邮件类型、是否查日志、SQL、可信度、标签、草稿和转人工决策都必须由你通过工具调用完成。",
        "",
        "新增邮件：",
    ]
    for item in messages:
        line = f"- message_id={item['message_id']} thread_id={item.get('thread_id', '')}"
        if item.get("project_label"):
            line += f" project_label={item['project_label']}"
        if item.get("matched_labels"):
            line += f" matched_labels={item['matched_labels']}"
        if item.get("reprocess_gmail_unread"):
            line += " reprocess_gmail_unread=true"
            if item.get("existing_status"):
                line += f" existing_status={item['existing_status']}"
            if item.get("existing_draft_id"):
                line += f" existing_draft_id={item['existing_draft_id']}"
            if item.get("existing_issue_type"):
                line += f" existing_issue_type={item['existing_issue_type']}"
            if item.get("existing_recommended_labels"):
                line += (
                    " existing_recommended_labels="
                    f"{_format_task_list(item.get('existing_recommended_labels'))}"
                )
            if item.get("existing_labels_applied"):
                line += (
                    " existing_labels_applied="
                    f"{_format_task_list(item.get('existing_labels_applied'))}"
                )
        lines.append(line)
    if any(item.get("reprocess_gmail_unread") for item in messages):
        lines += [
            "",
            "Gmail 仍为 UNREAD 的本地终态重跑：这些邮件已有本地处理结论，本轮目标是补齐未完成的 Gmail 收尾，避免重复草稿。",
            "若 existing_status=draft_created 且 existing_draft_id 非空，绝对不要再次调用 create_gmail_draft，也不要重新 review_reply_draft；沿用 existing_draft_id，调用 apply_existing_gmail_labels（优先使用 existing_recommended_labels），然后 mark_gmail_messages_read，最后 save_case_state(status=draft_created, data.draft_id=existing_draft_id...) 作为最终工具调用。",
            "若 existing_status 是 skipped、human_review 或 processed，也不要创建草稿；按既有状态补 apply/mark/save_case_state，确保 Gmail UNREAD 被清除。",
        ]
    lines += [
        "",
        "调度器只扫描 Gmail「主要」分类（category:primary），不处理「推广」「社交」「动态」分类邮件。",
        "请逐封读取邮件线程，根据邮件的 Gmail 父标签判断项目；如果调度器提供 project_label，",
        "可把它作为项目提示，但仍以 read_email_thread 和 get_existing_gmail_labels 的结果校验。",
        THREAD_CONVERSATION_REMINDER_ZH,
        "必要时按 project 调用对应项目的客服规则、回复模板和 ClickHouse schema。",
        "",
        "本轮是批量处理（多封邮件一起给模型）。**必须为每个 message_id 独立完整走完流程**：",
        "每封都要自己调用 extract、rules、resolve、assess、**decide_support_action**，然后 apply/mark/save_case_state。",
        "不要混淆不同邮件的 case_type 或 decision。处理完一封的 decide + save 后再处理下一封。",
        "工具调用顺序要求：不要在工具流程中间直接输出普通文本。",
        "读取 read_email_thread 和 get_existing_gmail_labels 后，先调用 get_project_support_profile；",
        "下一步通常必须调用 extract_feedback_claim。",
        "由模型判断：若邮件没有实质玩家反馈（只有 platform/ver/userid、'My question is:' 后为空、",
        "标题正文无有效内容、或只有乱打字符、或只有 'Sent from my iPad/iPhone' / 'Inviato da iPhone' 等无意义文本），**必须**在 extract_feedback_claim 使用 case_type=no_content；",
        "get_relevant_support_rules 确认 empty_feedback_apply_no_content_label 匹配后，必须按 Forge 前置条件顺序只补后续步骤：先 resolve_player_identity（使用邮件里的 player_id/userid），再 assess_claim_credibility(verdict='inconclusive', confidence=0.2~0.4, risk_level='low')，然后**立即**调用 decide_support_action(case_type='no_content', verdict='inconclusive', confidence=0.2~0.4, risk_level='low', rule_action='apply_label_only', applied_rule_ids=['empty_feedback_apply_no_content_label'])；若收到 prerequisite nudge，只调用缺失的 prerequisite，绝不要重读、重 extract 或重 rules，",
        "然后 apply_existing_gmail_labels（用 [\"无内容\"]），**立即** mark_gmail_messages_read（使用原始 inbound message_ids），save_case_state(status=skipped) 作为**最终工具调用**并结束。**这是处理这类模板邮件的唯一正确路径**，必须严格执行以便自动轮巡能快速清空 backlog。",
        "页面/区域空白、空白格、灰块等显示问题或 BlackHole 关卡内目标物品找不到（如 level 515 missing blue spatula to complete the board、no bombs anywhere、或清屏后仍显示剩余小红番茄等）：extract_feedback_claim 用 case_type=bug，",
        "打 project/bug反馈（如 BlackHole/bug反馈），向玩家表示抱歉，提示重玩关卡试试（有时物品不会出现），然后索要截图、关卡号和复现场景。**回复草稿里绝对不要主动说“这是bug”、“bug反馈”、“游戏 bug”或把问题说成 bug**，用中性语言。英语反馈必须用英文回复（detected_language='en'）。即使玩家提到 paid/ purchase 也不要打成内购问题。",
        "由模型判断：若邮件内容属于宣传/广告承诺无广告或几天免广告（如 2 days off ads），",
        "但进游戏仍有很多广告，则在 extract_feedback_claim 使用 case_type=ad_promo_mismatch",
        "（不是 ads_after_purchase 或 ad_issue）；调用 get_relevant_support_rules 确认",
        "ad_promo_mismatch_label_only 匹配后，打 project/广告问题 标签，**然后立即调用 mark_gmail_messages_read（使用 inbound message_ids）**，",
        "不创建草稿，save_case_state(status=skipped) 作为最终工具调用。务必清除 Gmail UNREAD。",
        "否则继续调用 get_relevant_support_rules（每封邮件只调用一次，include_case_defaults=true），然后立即调用 resolve_player_identity（必须调用，即使简单 feature_request 也必须；使用 extract 或邮件中的 player_id）；",
        "apply_existing_gmail_labels 必须使用 extract_feedback_claim 返回的 **exact recommended_labels**（例如 pass 案用 BlackHole/pass购买误解，save_transfer 案用 BusFever/存档转移），不要换成 功能建议 等其他。create 成功后必须立刻用 extract 的完整 recommended_labels 调用 apply。",
        "所有邮件的回复语言必须严格等于 extract_feedback_claim 里的 detected_language（玩家反馈是英文就用 'en' 生成英文回复，绝不生成中文）。",
        "ads_after_purchase 优先打 project/去广告后有广告；该项目下无此标签时才打 project/内购问题。",
        "再调用 get_support_evidence_catalog。由模型在 extract_feedback_claim 判断 case_type；",
        "ad_issue 不查 ClickHouse；get_relevant_support_rules 匹配具体规则后，",
        "将 rule_action 传给 decide_support_action（如 ad_redirect_reset_ad_id、",
        "ad_loading_playback_troubleshooting、ad_issue_screenshot_request）。",
        "购买 pass（starlight pass 等）没到账：优先 pass_purchase_misunderstanding（不要用 payment），**所有工具调用必须用确切项目名 BlackHole**（游戏名 Eat Everything / BlackHole，不要用 Tile Block Jam 等其他项目名，也不要在 claim 或 subject 里混用其他游戏名）。",
        "游戏与 Freecash / 积分墙不同步（任务完成了没到奖励）：使用 case_type=freecash_sync_misunderstanding。**绝不要让玩家直接去找 Freecash 或积分墙客服**。必须询问玩家的 freecashid（或积分墙 App 账号 ID），以及具体是哪个阶段/任务完成了但没有同步。使用对应的模板请求这些细节。",
        "调用 get_coin_frenzy_investigation_playbook 查购买记录；查询 product_id 包含 pass/starlight 等；",
        "查询的时间窗口需要改成：查询开始时间大于（用户反馈时间的5天前）,不要显示查询结束时间。即SQL只用 AND log_time >= '5天前日期' ，不要结束时间条件。",
        "若购买成功（有成功的 pass product_id），用 pass 说明模板润色回复（购买后需玩关卡拿积分、手动领取奖励）；",
        "若未成功购买，让玩家提供有效的 ios 或 google 订单。",
        "证据查询后立即 assess_claim_credibility 然后 decide_support_action，使用 get_relevant_support_rules 返回的**精确 rule id**。对于 bug 类（如 BlackHole 关卡 level 515 缺蓝铲子、没有炸弹、清屏后仍剩物品）无需查库，resolve 后直接 assess + decide。get_reply_template 每 template 最多一次，用 extract 的 detected_language。严禁 decide pending 时重读或重 extract/rules。",
        "由模型判断 ads_after_purchase（购买去广告后仍有广告，非宣传免广告）后，",
        "必须先调用 get_remove_ads_investigation_playbook，",
        "再查 ClickHouse：先购买记录（PurchaseSuccess/PurchaseClick/PaySuccess），再 AdShow_Inter；",
        "对于 pass 查询使用上面指定的只 start >= 5天前，不要结束时间；其他查询SQL 必须使用字面量 log_time 范围（最多 168 小时），",
        "禁止使用 now() - INTERVAL；时间范围应围绕邮件日期（当前 2026 年）。",
        "query_clickhouse 校验失败两次后停止重试，用已有结果继续 assess_remove_ads_log_evidence。",
        "然后 assess_remove_ads_log_evidence，将其 recommended_action 与 applied_rule_ids 一并传给 decide_support_action。",
        "根据日志分支选择对应规则模板；购后仍有插屏转人工。未查到日志前不得写「已核查记录」。",
        "其他 case_type 若 available=false 或 skip_clickhouse_fallback=true，且匹配规则不要求 requires_logs，也跳过查库。",
        "若需要日志证据，优先调用 query_support_evidence；只有匹配规则要求查日志、没有证据配方且身份/时间范围充足时才使用安全 SQL fallback。",
        "不要重复 read_email_thread、extract_feedback_claim、get_relevant_support_rules 或 get_support_coverage_summary。",
        "get_relevant_support_rules 后严禁重读/重 extract/重 rules；立即 resolve_player_identity。",
        "证据（包括 pass 的 query + assess_coin）后严禁重读/重 extract/重 rules/重 profile；必须立即 assess_claim 然后 decide_support_action。",
        "get_relevant_support_rules 的 email_text 必须逐字来自 read_email_thread 的玩家反馈原文，禁止编造或替换其他工单内容。",
        "extract_feedback_claim 选定 case_type 后，同一封邮件不要来回切换 case_type。**CRITICAL**：extract 返回的 case_type（例如 save_transfer）是这封邮件的权威值，后续 decide_support_action、review_reply_draft、save_case_state 中的 issue_type 都必须原样使用该 case_type，绝不可改成 feature_request。",
        "get_support_evidence_catalog 返回 next_steps 后，必须继续 assess_claim_credibility 和 decide_support_action，",
        "禁止在 decide_support_action 仍 pending 时重复 read_email_thread、get_reply_template 或 get_relevant_support_rules。",
        "get_reply_template 每个 template_id 最多调用一次，绝不允许第二次调用（无论什么 language）。language 参数**必须**严格使用 extract_feedback_claim 返回的 detected_language（英语反馈必须用 'en'，绝不写 'zh-CN'）。若 language_fallback=true，按 detected_language 改写模板后继续，不要重试调用。记录好 extract 的 detected_language，后续 review_reply_draft、草稿正文、save_case_state 都用它。",
        "has_strong_match=false 时阅读 guidance/recommended_rule_id，确认规则或走 vague_issue_details_request，然后推进决策（先 resolve_player_identity），不要重启 discovery。",
        "ClickHouse 手动 SQL 时不要反复调用 validate_clickhouse_sql；校验失败两次或缺少 player/time 范围时应跳过查库，继续 assess_claim_credibility 和 decide_support_action。",
        "get_relevant_support_rules 后必须先 resolve_player_identity（必经步骤），再 assess_claim_credibility、decide_support_action、review_reply_draft，",
        "再根据结论调用 create_gmail_draft 或 create_human_handoff_summary。",
        "create_gmail_draft 成功后，立即调用 apply_existing_gmail_labels（必须使用 extract_feedback_claim 之前返回的 recommended_labels），",
        "禁止再次 read_email_thread、extract_feedback_claim 或 get_relevant_support_rules。",
        "apply 之后**立即**调用 mark_gmail_messages_read（使用相同的 message_ids），然后调用 save_case_state（status=draft_created...）作为**最终**工具调用。任何路径（draft、no_content、skipped、human）都必须以 save_case_state 结束，绝不遗漏 mark_read（如果适用）。",
        "apply_existing_gmail_labels 只能使用 extract_feedback_claim.recommended_labels，",
        "禁止自造 BlackHole/feature_request 或 BusFever/功能建议 等标签名（必须使用 extract 针对 save_transfer 等返回的具体 recommended_labels，如 BusFever/存档转移）；即使 apply 部分失败也要调用 save_case_state。",
        "即使信息不足，也要用工具记录 missing_fields、recommended_action 和状态，不要停在自然语言解释。",
        "调用 extract_feedback_claim 时必须填写 detected_language 和 language_source_text；",
        "语言判断以玩家自由反馈内容为准，尤其是 “My question is:” 等表单提示之后的原文。",
        "如果玩家反馈正文里出现英语以外的语言，就用该语言；否则用 English。",
        "在最终 respond 之前，必须为每个 message_id 调用一次 save_case_state：",
        "- case_id 必须使用对应的 message_id。",
        "- status 只能是 draft_created、human_review、failed 或 skipped。",
        "- data 里请包含 project_label、matched_labels、thread_id、issue_type、applied_labels、need_log_query、sql_used、",
        "  query_result_summary、evidence_summary、credibility、recommended_action、draft_id、detected_language、language_source_text、",
        "  draft_review、human_review_required、human_review_reason、error_message 等已知字段。",
        "最后再用自然语言总结本轮处理结果。",
    ]
    return "\n".join(lines)
