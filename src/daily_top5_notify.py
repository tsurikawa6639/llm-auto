"""日次トップ5アイディアをDiscordに通知するスクリプト

`output/daily_top5/YYYY-MM-DD.md` をDiscordのアイデアチャンネルへ
1アイディア=1embedで送信する。ファイル形式は Claude routine が生成する Markdown 想定:

```
---
date: 2026-05-14
total_files_reviewed: 42
total_ideas_reviewed: 87
---

# 本日のトップ5投資アイディア (2026-05-14)

本日 42本のレポートから抽出した 87件のアイディアより、特に有望と判断した5件を厳選しました。

## 1. <タイトル>
- 📺 **動画**: [<動画タイトル>](<URL>) / <チャンネル名>
- 🎯 **要点**: ...
- 📌 **根拠**: ...
- 💡 **選定理由**: ...

## 2. ...
```
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

JST = timezone(timedelta(hours=9))
MAX_EMBED_DESCRIPTION = 4096
SLEEP_BETWEEN_POSTS = 1.0  # Discord rate-limit 緩衝
SUMMARY_EMBED_COLOR = 0x3498DB

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOP5_DIR = PROJECT_ROOT / "output" / "daily_top5"
CSV_DIR = PROJECT_ROOT / "output" / "csv"


def find_latest_top5() -> Path | None:
    if not TOP5_DIR.exists():
        return None
    candidates = sorted(TOP5_DIR.glob("*.md"))
    return candidates[-1] if candidates else None


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip()
    return text


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    result: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def count_monitored_videos(target_date: str) -> int:
    """target_date (YYYY-MM-DD) のJST日付に対応するCSVから unique URL 数を返す"""
    if not CSV_DIR.exists():
        return 0
    prefix = target_date.replace("-", "")
    unique_urls: set[str] = set()
    for csv_path in CSV_DIR.glob(f"report_{prefix}_*.csv"):
        try:
            with csv_path.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    url = (row.get("url") or "").strip()
                    if url:
                        unique_urls.add(url)
        except OSError as e:
            logger.warning(f"CSV読み込み失敗 {csv_path.name}: {e}")
    return len(unique_urls)


def _safe_int(value: str | None) -> int:
    try:
        return int((value or "").strip())
    except (TypeError, ValueError):
        return 0


def build_summary_embed(date_str: str, monitored: int, ideas: int) -> dict:
    lines = [
        f"📺 **監視動画**: {monitored:,}本",
        f"💡 **抽出アイディア**: {ideas:,}件",
    ]
    return {
        "embeds": [
            {
                "title": f"📊 本日のサマリー ({date_str})",
                "description": "\n".join(lines),
                "color": SUMMARY_EMBED_COLOR,
                "footer": {"text": f"YouTube定刻監視 — 日次サマリー ({date_str})"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def parse_top5_sections(text: str) -> list[dict]:
    """Markdownを `## N. <title>` 単位に分割して返す

    Returns:
        [{"rank": int, "title": str, "body": str}, ...]
    """
    body = strip_frontmatter(text)
    pattern = re.compile(r"^## (\d+)\.\s*(.+?)$", re.MULTILINE)
    matches = list(pattern.finditer(body))

    sections: list[dict] = []
    for i, m in enumerate(matches):
        rank = int(m.group(1))
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section_body = body[start:end].strip()
        sections.append({"rank": rank, "title": title, "body": section_body})
    return sections


def post_embed(webhook_url: str, payload: dict, label: str) -> bool:
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
    except requests.RequestException as e:
        logger.error(f"Discord送信エラー ({label}): {e}")
        return False

    if resp.status_code in (200, 204):
        logger.info(f"Discord送信成功: {label}")
        return True
    logger.warning(
        f"Discord送信失敗 ({label}): status={resp.status_code} body={resp.text[:200]}"
    )
    return False


def send_to_discord(webhook_url: str, file_path: Path) -> int:
    """1アイディア=1embedで送信し、最後にサマリーembedを追加する。送信成功した件数を返す"""
    raw = file_path.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(raw)

    # 日付はファイル名から抽出（YYYY-MM-DD.md）
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})", file_path.stem)
    date_str = date_match.group(1) if date_match else file_path.stem

    sections = parse_top5_sections(raw)
    if not sections:
        logger.warning("トップ5セクションが見つかりませんでした。本文全体を1embedで送信します。")
        # フォールバック: 本文全体を1embedで送る
        body = strip_frontmatter(raw)
        payload = {
            "embeds": [
                {
                    "title": f"🏆 本日のトップ5投資アイディア ({date_str})",
                    "description": truncate(body, MAX_EMBED_DESCRIPTION),
                    "color": 0xF1C40F,
                    "footer": {"text": f"YouTube定刻監視 — 日次トップ5 ({date_str})"},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }
        return 1 if post_embed(webhook_url, payload, "fallback") else 0

    total = len(sections)
    sent = 0
    for sec in sections:
        rank = sec["rank"]
        title = sec["title"]
        body = sec["body"]

        payload = {
            "embeds": [
                {
                    "title": f"💡 [{rank}/{total}] {title}",
                    "description": truncate(body, MAX_EMBED_DESCRIPTION),
                    "color": 0xF1C40F,
                    "footer": {"text": f"YouTube定刻監視 — 日次トップ{total} ({date_str})"},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }

        if post_embed(webhook_url, payload, f"#{rank} {title[:30]}"):
            sent += 1

        time.sleep(SLEEP_BETWEEN_POSTS)

    # サマリー embed: 監視動画数(CSVから集計) と 抽出アイディア数(frontmatterから)
    monitored = count_monitored_videos(date_str)
    if monitored == 0:
        monitored = _safe_int(frontmatter.get("total_files_reviewed"))
    ideas = _safe_int(frontmatter.get("total_ideas_reviewed"))

    summary_payload = build_summary_embed(date_str, monitored, ideas)
    if post_embed(webhook_url, summary_payload, "summary"):
        sent += 1

    logger.info(f"送信完了: {sent}/{total + 1}件")
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="日次トップ5アイディアをDiscordへ通知")
    parser.add_argument("--file", help="送信するMarkdownファイルパス（未指定なら最新を自動選択）")
    args = parser.parse_args()

    webhook_url = os.getenv("DISCORD_IDEA_WEBHOOK_URL", "")
    if not webhook_url:
        logger.error("DISCORD_IDEA_WEBHOOK_URL が設定されていません")
        return 1

    if args.file:
        target = Path(args.file)
    else:
        target = find_latest_top5()

    if not target or not target.exists():
        logger.error("送信対象のMarkdownファイルが見つかりません")
        return 1

    sent = send_to_discord(webhook_url, target)
    return 0 if sent > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
