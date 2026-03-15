"""Discord Webhook 通知モジュール

監視レポートのサマリーと投資アイデアをDiscordに送信する。
- サマリーチャンネル: 実行完了ごとに1投稿
- アイデアチャンネル: アイデア1件につき1投稿
"""

import logging
import re
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)

# 日本標準時 (JST = UTC+9)
JST = timezone(timedelta(hours=9))

# Discord Embed の文字数上限
MAX_EMBED_DESCRIPTION = 4096
MAX_FIELD_VALUE = 1024


class DiscordNotifier:
    """Discord Webhook で通知を送信する"""

    def __init__(self, summary_webhook_url: str = "", idea_webhook_url: str = ""):
        self.summary_webhook_url = summary_webhook_url or ""
        self.idea_webhook_url = idea_webhook_url or ""

    # ------------------------------------------------------------------
    # サマリー通知（実行完了ごとに1回）
    # ------------------------------------------------------------------
    def send_summary(
        self,
        results: list[dict],
        total_processed: int,
        total_ideas: int,
    ) -> bool:
        """監視レポートのサマリーをサマリーチャンネルに送信する

        Returns:
            送信成功なら True、スキップまたは失敗なら False
        """
        if not self.summary_webhook_url:
            logger.info("Discord サマリー Webhook URL 未設定 — 送信スキップ")
            return False

        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

        # アイデアありの動画だけ抽出
        idea_lines = []
        for row in results:
            if row.get("idea", "").startswith("✅"):
                idea_lines.append(
                    f"✅ **{row['title']}**\n"
                    f"　📺 {row.get('channel', '不明')}\n"
                    f"　🔗 {row.get('url', '')}"
                )

        description_parts = [
            f"**実行日時**: {now}",
            f"**調査動画数**: {total_processed}件 | **アイディア**: {total_ideas}件",
        ]
        if idea_lines:
            description_parts.append("")
            description_parts.extend(idea_lines)
        else:
            description_parts.append("\nアイディアは見つかりませんでした。")

        description = "\n".join(description_parts)
        # 文字数制限
        if len(description) > MAX_EMBED_DESCRIPTION:
            description = description[: MAX_EMBED_DESCRIPTION - 3] + "..."

        payload = {
            "embeds": [
                {
                    "title": "📊 YouTube監視レポート",
                    "description": description,
                    "color": 0x3498DB,  # 青
                    "footer": {"text": "YouTube定刻監視システム"},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }

        return self._post(self.summary_webhook_url, payload, "サマリー")

    # ------------------------------------------------------------------
    # アイデア個別通知（1アイデア = 1投稿）
    # ------------------------------------------------------------------
    def send_idea(
        self,
        video_info: dict,
        summary: str,
        idea_text: str,
    ) -> bool:
        """投資アイデアをアイデアチャンネルに送信する

        Args:
            video_info: 動画情報 (title, channel, published_at, video_id, etc.)
            summary: 動画の要約テキスト
            idea_text: Gemini が生成したアイデアテキスト（Markdown形式）

        Returns:
            送信成功なら True、スキップまたは失敗なら False
        """
        if not self.idea_webhook_url:
            logger.info("Discord アイデア Webhook URL 未設定 — 送信スキップ")
            return False

        title = video_info.get("title", "不明")
        channel = video_info.get("channel", "不明")
        video_id = video_info.get("video_id", "")
        url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
        published_at = self._to_jst(video_info.get("published_at", ""))
        view_count = video_info.get("view_count", "")

        # アイデアテキストからタイトルを抽出（# で始まる行）
        idea_title = self._extract_idea_title(idea_text)

        # セクション分割
        sections = self._parse_idea_sections(idea_text)

        # --- Embed 構築 ---
        # ソース情報
        source_lines = [
            f"YouTube - {channel}「{title}」",
            f"公開日: {published_at}",
        ]
        if view_count:
            source_lines[-1] += f" | 再生数: {view_count}回"

        fields = [
            {
                "name": "📺 データソース",
                "value": "\n".join(source_lines),
                "inline": False,
            },
            {
                "name": "📝 要約",
                "value": self._truncate(summary, MAX_FIELD_VALUE),
                "inline": False,
            },
        ]

        # 投資アイディア
        idea_body = sections.get("投資アイディア", "")
        if idea_body:
            fields.append({
                "name": "🎯 投資アイディア",
                "value": self._truncate(idea_body, MAX_FIELD_VALUE),
                "inline": False,
            })

        # 根拠
        evidence = sections.get("根拠となった個所", "")
        if evidence:
            fields.append({
                "name": "📌 根拠",
                "value": self._truncate(evidence, MAX_FIELD_VALUE),
                "inline": False,
            })

        # 因果関係
        causal = sections.get("因果関係", "")
        if causal:
            fields.append({
                "name": "🔗 因果関係",
                "value": self._truncate(causal, MAX_FIELD_VALUE),
                "inline": False,
            })

        # URL
        if url:
            fields.append({
                "name": "🔗 動画リンク",
                "value": url,
                "inline": False,
            })

        payload = {
            "embeds": [
                {
                    "title": f"💡 {idea_title}",
                    "color": 0xF1C40F,  # 金色
                    "fields": fields,
                    "footer": {"text": "YouTube定刻監視システム"},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }

        return self._post(self.idea_webhook_url, payload, "アイデア")

    # ------------------------------------------------------------------
    # ユーティリティ
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_idea_title(idea_text: str) -> str:
        """アイデアテキストから `# タイトル` 形式のタイトルを抽出する"""
        for line in idea_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                return stripped[2:].strip()
        return "投資アイデア"

    @staticmethod
    def _parse_idea_sections(idea_text: str) -> dict[str, str]:
        """Markdown の ## 見出しでセクション分割する"""
        sections: dict[str, str] = {}
        current_key = ""
        current_lines: list[str] = []

        for line in idea_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                # 前のセクションを保存
                if current_key:
                    sections[current_key] = "\n".join(current_lines).strip()
                current_key = stripped[3:].strip()
                current_lines = []
            else:
                current_lines.append(line)

        # 最後のセクションを保存
        if current_key:
            sections[current_key] = "\n".join(current_lines).strip()

        return sections

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        """テキストを最大長に切り詰める"""
        if not text:
            return "ー"
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    @staticmethod
    def _to_jst(utc_str: str) -> str:
        """UTC文字列 (例: '2026-03-14T11:00:01Z') をJST文字列に変換する"""
        if not utc_str:
            return "不明"
        try:
            dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
            return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST")
        except (ValueError, AttributeError):
            return utc_str

    @staticmethod
    def _post(webhook_url: str, payload: dict, label: str) -> bool:
        """Webhook に POST する"""
        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
            if resp.status_code in (200, 204):
                logger.info(f"Discord {label}通知 送信成功")
                return True
            else:
                logger.warning(
                    f"Discord {label}通知 送信失敗: "
                    f"status={resp.status_code} body={resp.text[:200]}"
                )
                return False
        except requests.RequestException as e:
            logger.error(f"Discord {label}通知 送信エラー: {e}")
            return False
