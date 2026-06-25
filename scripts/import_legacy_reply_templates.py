#!/usr/bin/env python3
"""Import human-authored reply templates from Desktop folder into the agent KB."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path.home() / "Desktop" / "回复模板"

FILE_META: dict[str, dict] = {
    "BlackHole反馈.txt": {
        "projects": ["BlackHole"],
        "prefix": "blackhole",
        "default_case_types": ["gameplay_misunderstanding", "bug", "feature_request", "other"],
        "template_root": "templates/projects/BlackHole",
    },
    "BusFever反馈.txt": {
        "projects": ["BusFever"],
        "prefix": "busfever",
        "default_case_types": ["gameplay_misunderstanding", "bug", "crash_or_freeze", "other"],
        "template_root": "templates/projects/BusFever",
    },
    "NumberCrash反馈.txt": {
        "projects": ["NumberCrush"],
        "prefix": "numbercrush",
        "default_case_types": ["gameplay_misunderstanding", "bug", "feature_request", "other"],
        "template_root": "templates/projects/NumberCrush",
    },
    "广告反馈.txt": {
        "projects": [],
        "prefix": "legacy_ad",
        "default_case_types": ["ad_issue", "ads_after_purchase"],
        "template_root": "templates/replies",
    },
    "支付反馈.txt": {
        "projects": [],
        "prefix": "legacy_payment",
        "default_case_types": ["payment", "ads_after_purchase", "pass_purchase_misunderstanding"],
        "template_root": "templates/replies",
    },
    "存档相关.txt": {
        "projects": [],
        "prefix": "legacy_save",
        "default_case_types": ["lost_save", "save_transfer", "account_binding"],
        "template_root": "templates/replies",
    },
    "烤串反馈.txt": {
        "projects": ["Grill Master"],
        "prefix": "grillmaster",
        "default_case_types": ["gameplay_misunderstanding", "bug", "ad_issue", "other"],
        "template_root": "templates/projects/Grill Master",
    },
    "反馈邮件.txt": {
        "projects": [],
        "prefix": "legacy_mail",
        "default_case_types": [],
        "template_root": "templates/replies",
    },
    "商店评价.txt": {
        "projects": [],
        "prefix": "legacy_store_review",
        "default_case_types": ["store_review"],
        "template_root": "templates/replies",
    },
}

POLISH_PREFIX_RE = re.compile(
    r"^(?:润色并翻译[：:；;]\s*|润色并翻译\s*)",
    re.IGNORECASE,
)
NUMBERED_SCENARIO_RE = re.compile(r"^\d+[、.]\s*(.+?)[：:]\s*$")
Greeting_STARTERS = (
    "您好",
    "你好",
    "Hello",
    "Hi ",
    "Hi!",
    "感谢",
    "明白",
    "不客气",
    "再次感谢",
    "对于",
    "我们",
    "目前",
    "如果您",
    "请问",
    "很抱歉",
    "得知",
    "应该是",
    "以我的",
    "您如果",
    "您从",
    "以下是",
    "详细流程",
    "方法一",
    "方法二",
    "方法三",
    "退款完成",
    "更新版本",
    "纯瞎想",
    "小作坊",
    "游戏内",
    "如果觉得",
)


def _slugify(value: str, *, max_len: int = 48) -> str:
    value = POLISH_PREFIX_RE.sub("", value).strip()
    ascii_parts = re.findall(r"[a-z0-9]+", value.casefold())
    if ascii_parts:
        cleaned = "_".join(ascii_parts)
    else:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
        cleaned = f"topic_{digest}"
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if len(cleaned) > max_len:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned[: max_len - 9]}_{digest}"
    return cleaned or "entry"


def _extract_triggers(title: str | None, body: str) -> list[str]:
    triggers: list[str] = []
    if title:
        for part in re.split(r"[/、,，\s]+", title):
            part = part.strip()
            if 2 <= len(part) <= 24:
                triggers.append(part)
    keyword_patterns = [
        r"去广告",
        r"restore",
        r"存档",
        r"广告",
        r"rv",
        r"内购",
        r"退款",
        r"订单",
        r"pass",
        r"vip",
        r"卡死",
        r"闪退",
        r"卡顿",
        r"卡册",
        r"连胜",
        r"金币",
        r"磁铁",
        r"炸弹",
        r"消防员",
        r"车位",
        r"烤串",
        r"userid",
        r"横竖屏",
        r"断网",
        r"云存档",
        r"删除账号",
        r"quorde",
        r"piggybank",
        r"superbundle",
    ]
    sample = f"{title or ''}\n{body[:400]}"
    for pattern in keyword_patterns:
        if re.search(pattern, sample, re.IGNORECASE):
            triggers.append(pattern.lower() if pattern.isascii() else pattern)
    deduped: list[str] = []
    seen: set[str] = set()
    for trigger in triggers:
        key = trigger.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(trigger)
    return deduped[:12]


def _split_blocks(text: str) -> list[tuple[str | None, str]]:
    raw_blocks = re.split(r"\n\s*\n+", text.strip())
    entries: list[tuple[str | None, str]] = []
    pending_title: str | None = None

    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        first = lines[0].strip()
        numbered = NUMBERED_SCENARIO_RE.match(first)
        if numbered and len(lines) > 1:
            body = "\n".join(lines[1:]).strip()
            entries.append((numbered.group(1).strip(), POLISH_PREFIX_RE.sub("", body).strip()))
            continue

        if (
            len(first) <= 36
            and not any(first.startswith(prefix) for prefix in Greeting_STARTERS)
            and len(lines) > 1
        ):
            body = "\n".join(lines[1:]).strip()
            entries.append((first, POLISH_PREFIX_RE.sub("", body).strip()))
            continue

        if pending_title and len(first) <= 36:
            entries.append((pending_title, POLISH_PREFIX_RE.sub("", block).strip()))
            pending_title = None
            continue

        if len(first) <= 24 and not any(first.startswith(prefix) for prefix in Greeting_STARTERS):
            pending_title = first
            if len(lines) == 1:
                continue
            body = "\n".join(lines[1:]).strip()
            entries.append((first, POLISH_PREFIX_RE.sub("", body).strip()))
            pending_title = None
            continue

        entries.append((pending_title, POLISH_PREFIX_RE.sub("", block).strip()))
        pending_title = None

    return [(title, body) for title, body in entries if body and len(body) >= 20]


def _render_catalog(entries: list[dict]) -> str:
    lines = [
        "# Auto-generated legacy reply template catalog.",
        "# Source: Desktop/回复模板 — re-run scripts/import_legacy_reply_templates.py to refresh.",
        "",
    ]
    for entry in entries:
        lines.append("[[templates]]")
        lines.append(f'id = "{entry["id"]}"')
        if entry.get("summary"):
            summary = entry["summary"].replace('"', '\\"')
            lines.append(f'summary = "{summary}"')
        if entry.get("projects"):
            projects = ", ".join(f'"{p}"' for p in entry["projects"])
            lines.append(f"projects = [{projects}]")
        if entry.get("case_types"):
            case_types = ", ".join(f'"{c}"' for c in entry["case_types"])
            lines.append(f"case_types = [{case_types}]")
        if entry.get("triggers"):
            triggers = ", ".join(f'"{t}"' for t in entry["triggers"])
            lines.append(f"triggers = [{triggers}]")
        lines.append(f'body_path = "{entry["body_path"]}"')
        lines.append(f"priority = {entry.get('priority', 45)}")
        lines.append(f'source_file = "{entry["source_file"]}"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


LEGACY_TEMPLATE_PREFIXES = (
    "legacy_",
    "blackhole_",
    "busfever_",
    "numbercrush_",
    "grillmaster_",
)


def _legacy_template_dirs() -> list[Path]:
    dirs: list[Path] = []
    for meta in FILE_META.values():
        dirs.append(ROOT / meta["template_root"] / "zh-CN")
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in dirs:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


def _cleanup_orphan_templates(
    catalog_entries: list[dict],
    *,
    dry_run: bool = False,
) -> list[Path]:
    """Remove legacy-import .md files that are no longer referenced by the catalog."""

    catalog_paths = {ROOT / entry["body_path"] for entry in catalog_entries}
    removed: list[Path] = []
    for template_dir in _legacy_template_dirs():
        if not template_dir.exists():
            continue
        for path in template_dir.glob("*.md"):
            if not path.name.startswith(LEGACY_TEMPLATE_PREFIXES):
                continue
            if path in catalog_paths:
                continue
            removed.append(path)
            if not dry_run:
                path.unlink()
    return removed


def import_templates(source_dir: Path, *, dry_run: bool = False) -> list[dict]:
    catalog_entries: list[dict] = []
    used_ids: set[str] = set()

    for filename, meta in FILE_META.items():
        source_path = source_dir / filename
        if not source_path.exists():
            print(f"skip missing: {source_path}")
            continue
        text = source_path.read_text(encoding="utf-8")
        blocks = _split_blocks(text)
        template_root = ROOT / meta["template_root"] / "zh-CN"
        prefix = meta["prefix"]

        for index, (title, body) in enumerate(blocks, start=1):
            title_slug = _slugify(title or f"entry_{index}")
            template_id = f"{prefix}_{title_slug}"
            while template_id in used_ids:
                template_id = f"{prefix}_{title_slug}_{index}"
            used_ids.add(template_id)

            rel_path = (
                Path(meta["template_root"]) / "zh-CN" / f"{template_id}.md"
            ).as_posix()
            abs_path = ROOT / rel_path
            summary = (title or body.splitlines()[0])[:120]

            catalog_entries.append(
                {
                    "id": template_id,
                    "summary": summary,
                    "projects": meta["projects"],
                    "case_types": meta["default_case_types"],
                    "triggers": _extract_triggers(title, body),
                    "body_path": rel_path,
                    "priority": 50 if meta["projects"] else 45,
                    "source_file": filename,
                }
            )
            if not dry_run:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(body.strip() + "\n", encoding="utf-8")

        print(f"imported {len(blocks)} templates from {filename}")

    if not dry_run:
        catalog_path = ROOT / "knowledge" / "legacy_reply_templates.toml"
        catalog_path.write_text(_render_catalog(catalog_entries), encoding="utf-8")
        print(f"wrote catalog: {catalog_path} ({len(catalog_entries)} entries)")
        removed = _cleanup_orphan_templates(catalog_entries)
        if removed:
            print(f"removed {len(removed)} orphan template files")

    return catalog_entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.source.exists():
        raise SystemExit(f"Source folder not found: {args.source}")
    entries = import_templates(args.source.expanduser(), dry_run=args.dry_run)
    print(f"total templates: {len(entries)}")


if __name__ == "__main__":
    main()