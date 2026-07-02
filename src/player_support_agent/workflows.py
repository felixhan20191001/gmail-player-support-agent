"""Forge workflow definitions for the player support agent."""

from __future__ import annotations

from forge import Workflow
from forge import respond_tool

from .dry_run import apply_dry_run
from .prompts import MULTI_PROJECT_INTERACTIVE_CHAT_PROMPT, MULTI_PROJECT_SUPPORT_PROMPT
from .tools.config import SupportAgentConfig
from .tools.forge_tools import ToolSurface, build_tool_defs


def build_multi_project_workflow(
    config: SupportAgentConfig,
    *,
    dry_run: bool = True,
    allow_db_in_dry_run: bool = True,
    surface: ToolSurface = "auto",
) -> Workflow:
    """Build the end-to-end multi-project support workflow."""

    tools = build_tool_defs(config, surface=surface)
    if dry_run:
        tools = apply_dry_run(tools, allow_db=allow_db_in_dry_run)
    required_steps = [] if surface == "cleanup" else [
        "read_email_thread",
        "get_existing_gmail_labels",
        "get_project_support_profile",
        "extract_feedback_claim",
        "get_relevant_support_rules",
        "resolve_player_identity",
        "assess_claim_credibility",
        "decide_support_action",
    ]
    return Workflow(
        name="multi_project_support",
        description=(
            "Classify a project-labeled Gmail support thread, inspect "
            "project-specific ClickHouse logs when possible, and create a "
            "draft or human handoff."
        ),
        tools=tools,
        required_steps=required_steps,
        terminal_tool="save_case_state",
        system_prompt_template=MULTI_PROJECT_SUPPORT_PROMPT,
    )


def build_multi_project_chat_workflow(
    config: SupportAgentConfig,
    *,
    dry_run: bool = True,
    allow_db_in_dry_run: bool = True,
    required_steps: list[str] | None = None,
) -> Workflow:
    """Build a free-form interactive support-chat workflow."""

    tools = build_tool_defs(config, surface="chat")
    if dry_run:
        tools = apply_dry_run(tools, allow_db=allow_db_in_dry_run)
    tools["respond"] = respond_tool()
    return Workflow(
        name="multi_project_support_chat",
        description=(
            "Answer interactive multi-project support questions by letting the "
            "selected model decide which Gmail, ClickHouse, rules, or draft tools "
            "to call."
        ),
        tools=tools,
        required_steps=required_steps or [],
        terminal_tool="respond",
        system_prompt_template=MULTI_PROJECT_INTERACTIVE_CHAT_PROMPT,
    )
