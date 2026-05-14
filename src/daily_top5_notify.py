"""日次トップ5アイディアをDiscordに通知するスクリプト

`output/daily_top5/YYYY-MM-DD.md` をDiscordの要約チャンネルへ送信する。
ファイル形式は Claude routine が生成する Markdown 想定:

```
---
date: 2026-05-14
total_ideas_reviewed: 42
---

# 本日のトップ5アイディア

## 1. <タイトル>
- **動画**: <タイトル> ([URL])
- **チャンネル**: <チャンネル名>
- **要点**: <一文>
- **選出理由**: <一文>

## 2. ...
```
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

JST = timezone(timedelta(hours=9))
MAX_EMBED_DESCRIPTION = 4096

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOP5_DIR = PROJECT_ROOT / "output" / "daily_top5"


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


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def send_to_discord(webhook_url: str, file_path: Path) -> bool:
    raw = file_path.read_text(encoding="utf-8")
    body = strip_frontmatter(raw)

    # 日付をファイル名から抽出（YYYY-MM-DD.md）
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})", file_path.stem)
    date_str = date_match.group(1) if date_match else file_path.stem

    description = truncate(body, MAX_EMBED_DESCRIPTION)

    payload = {
        "embeds": [
            {
                "title": f"🏆 本日のトップ5投資アイディア ({date_str})",
                "description": description,
                "color": 0xF1C40F,
                "footer": {"text": "YouTube定刻監視システム — 日次トップ5"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
    except requests.RequestException as e:
        logger.error(f"Discord送信エラー: {e}")
        return False

    if resp.status_code in (200, 204):
        logger.info(f"Discord送信成功: {file_path.name}")
        return True
    logger.warning(f"Discord送信失敗: status={resp.status_code} body={resp.text[:200]}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="日次トップ5アイディアをDiscordへ通知")
    parser.add_argument("--file", help="送信するMarkdownファイルパス（未指定なら最新を自動選択）")
    args = parser.parse_args()

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        logger.error("DISCORD_WEBHOOK_URL が設定されていません")
        return 1

    if args.file:
        target = Path(args.file)
    else:
        target = find_latest_top5()

    if not target or not target.exists():
        logger.error("送信対象のMarkdownファイルが見つかりません")
        return 1

    ok = send_to_discord(webhook_url, target)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
