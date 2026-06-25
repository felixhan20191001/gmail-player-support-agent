"""Human handoff and notification tools."""

from __future__ import annotations

import json
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx

from forge.errors import ToolResolutionError

from .config import NotifyConfig


class NotifyTools:
    """Notify human support through file, webhook, Feishu, or SMTP."""

    def __init__(self, config: NotifyConfig) -> None:
        self.config = config

    def create_human_handoff_summary(
        self,
        case_id: str,
        email_subject: str,
        player_summary: str,
        claim_summary: str,
        evidence_summary: dict[str, Any],
        ai_recommendation: str,
        draft_reply: str | None = None,
    ) -> dict[str, Any]:
        """Create a compact handoff summary for a human reviewer."""

        summary = {
            "case_id": case_id,
            "email_subject": email_subject,
            "player_summary": player_summary,
            "claim_summary": claim_summary,
            "evidence_summary": evidence_summary,
            "ai_recommendation": ai_recommendation,
            "draft_reply": draft_reply,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        text = (
            f"Case: {case_id}\n"
            f"Subject: {email_subject}\n"
            f"Player: {player_summary}\n"
            f"Claim: {claim_summary}\n"
            f"Recommendation: {ai_recommendation}\n\n"
            f"Evidence:\n{json.dumps(evidence_summary, ensure_ascii=False, indent=2)}"
        )
        if draft_reply:
            text += f"\n\nDraft reply:\n{draft_reply}"
        return {"summary": summary, "text": text}

    async def notify_human_support(
        self,
        case_id: str,
        subject: str,
        summary_text: str,
        priority: str = "normal",
    ) -> dict[str, Any]:
        """Notify a human support reviewer."""

        if self.config.mode == "none":
            return {"notified": False, "mode": "none", "case_id": case_id}
        if self.config.mode == "file":
            return self._notify_file(case_id, subject, summary_text, priority)
        if self.config.mode == "webhook":
            return await self._notify_webhook(case_id, subject, summary_text, priority)
        if self.config.mode == "feishu":
            return await self._notify_feishu(case_id, subject, summary_text, priority)
        if self.config.mode == "smtp":
            return self._notify_smtp(case_id, subject, summary_text, priority)
        raise ToolResolutionError(f"Unsupported notify mode: {self.config.mode}")

    def _notify_file(
        self,
        case_id: str,
        subject: str,
        summary_text: str,
        priority: str,
    ) -> dict[str, Any]:
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{case_id}.txt"
        path.write_text(
            f"Priority: {priority}\nSubject: {subject}\n\n{summary_text}\n",
            encoding="utf-8",
        )
        return {"notified": True, "mode": "file", "path": str(path)}

    async def _notify_webhook(
        self,
        case_id: str,
        subject: str,
        summary_text: str,
        priority: str,
    ) -> dict[str, Any]:
        if not self.config.webhook_url:
            raise ToolResolutionError("notify.webhook_url is required")
        headers = {"Content-Type": "application/json"}
        token = self.config.resolve_webhook_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {
            "case_id": case_id,
            "subject": subject,
            "summary": summary_text,
            "priority": priority,
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                self.config.webhook_url,
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
        return {"notified": True, "mode": "webhook", "status_code": resp.status_code}

    async def _notify_feishu(
        self,
        case_id: str,
        subject: str,
        summary_text: str,
        priority: str,
    ) -> dict[str, Any]:
        url = self.config.feishu_webhook_url or self.config.webhook_url
        if not url:
            raise ToolResolutionError(
                "notify.feishu_webhook_url or notify.webhook_url is required"
            )
        text = (
            f"[{priority}] {subject}\n"
            f"Case: {case_id}\n\n"
            f"{summary_text}"
        )
        payload = {
            "msg_type": "text",
            "content": {"text": text},
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        return {"notified": True, "mode": "feishu", "status_code": resp.status_code}

    def _notify_smtp(
        self,
        case_id: str,
        subject: str,
        summary_text: str,
        priority: str,
    ) -> dict[str, Any]:
        if not self.config.smtp_host:
            raise ToolResolutionError("notify.smtp_host is required")
        if not self.config.human_support_email:
            raise ToolResolutionError("notify.human_support_email is required")

        username = self.config.resolve_smtp_username()
        password = self.config.resolve_smtp_password()
        from_addr = self.config.smtp_from or username
        if not from_addr:
            raise ToolResolutionError("notify.smtp_from or smtp username is required")

        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = self.config.human_support_email
        msg["Subject"] = f"[{priority}] Player support handoff: {subject}"
        msg.set_content(f"Case: {case_id}\n\n{summary_text}")

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port) as smtp:
            smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
        return {"notified": True, "mode": "smtp", "to": self.config.human_support_email}
