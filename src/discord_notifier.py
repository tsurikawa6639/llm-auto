"""Discord Webhook 通知モジュール

監視レポートのサマリーと投資アイデアをDiscordに送信する。
- サマリーチャンネル: 実行完了ごとに1投稿
- アイデアチャンネル: アイデア1件につき1投稿
- 遅延送信: 通知をJSONファイルに保存し、後からまとめて送信可能
"""

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
        # 遅延送信用キュー
        self._deferred_queue: list[dict] = []

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
    # スキップ通知（動画URL処理失敗時）
    # ------------------------------------------------------------------
    def send_skip(self, video_info: dict) -> bool:
        """動画処理失敗のスキップ通知をサマリーチャンネルに送信する"""
        if not self.summary_webhook_url:
            return False

        title = video_info.get("title", "不明")
        channel = video_info.get("channel", "不明")
        video_id = video_info.get("video_id", "")
        url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""

        payload = {
            "embeds": [
                {
                    "title": "⏭️ 動画処理スキップ",
                    "description": (
                        f"動画URLの処理に失敗したためスキップしました。\n\n"
                        f"**動画**: {title}\n"
                        f"**チャンネル**: {channel}\n"
                        f"**URL**: {url}"
                    ),
                    "color": 0x95A5A6,  # グレー
                    "footer": {"text": "YouTube定刻監視システム"},
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }

        return self._post(self.summary_webhook_url, payload, "スキップ")

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

    # ------------------------------------------------------------------
    # 遅延送信（バッチ送信）機能
    # ------------------------------------------------------------------
    def queue_idea(
        self,
        video_info: dict,
        summary: str,
        idea_text: str,
    ) -> None:
        """アイデア通知をキューに追加する（即座に送信しない）"""
        self._deferred_queue.append({
            "type": "idea",
            "video_info": video_info,
            "summary": summary,
            "idea_text": idea_text,
        })
        logger.info(f"Discord アイデア通知をキューに追加: {video_info.get('title', '不明')}")

    def queue_skip(self, video_info: dict) -> None:
        """スキップ通知をキューに追加する（即座に送信しない）"""
        self._deferred_queue.append({
            "type": "skip",
            "video_info": video_info,
        })
        logger.info(f"Discord スキップ通知をキューに追加: {video_info.get('title', '不明')}")

    def queue_summary(
        self,
        results: list[dict],
        total_processed: int,
        total_ideas: int,
    ) -> None:
        """サマリー通知をキューに追加する（即座に送信しない）"""
        self._deferred_queue.append({
            "type": "summary",
            "results": results,
            "total_processed": total_processed,
            "total_ideas": total_ideas,
        })
        logger.info("Discord サマリー通知をキューに追加")

    def save_deferred(self, filepath: str | Path) -> None:
        """キューに溜まった通知をJSONファイルに保存する

        Args:
            filepath: 保存先のJSONファイルパス
        """
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        if not self._deferred_queue:
            logger.info("遅延送信キューが空のため保存スキップ")
            return

        filepath.write_text(
            json.dumps(self._deferred_queue, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"遅延通知を保存: {filepath} ({len(self._deferred_queue)}件)")

    def send_deferred(self, filepath: str | Path) -> int:
        """保存済みの遅延通知を読み込んで一括送信する

        Args:
            filepath: 遅延通知JSONファイルのパス

        Returns:
            送信成功した通知の件数
        """
        filepath = Path(filepath)
        if not filepath.exists():
            logger.info("遅延通知ファイルなし — 送信スキップ")
            return 0

        try:
            queue = json.loads(filepath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"遅延通知ファイルの読み込みエラー: {e}")
            return 0

        if not queue:
            logger.info("遅延通知キューが空 — 送信スキップ")
            filepath.unlink(missing_ok=True)
            return 0

        logger.info(f"遅延通知を一括送信開始: {len(queue)}件")
        sent = 0

        for item in queue:
            msg_type = item.get("type", "")
            success = False

            if msg_type == "idea":
                success = self.send_idea(
                    item["video_info"],
                    item["summary"],
                    item["idea_text"],
                )
            elif msg_type == "skip":
                success = self.send_skip(item["video_info"])
            elif msg_type == "summary":
                success = self.send_summary(
                    item["results"],
                    item["total_processed"],
                    item["total_ideas"],
                )
            else:
                logger.warning(f"不明な通知タイプ: {msg_type}")
                continue

            if success:
                sent += 1

            # Discord レート制限回避のため少し待機
            time.sleep(1)

        # 送信完了後にファイル削除
        filepath.unlink(missing_ok=True)
        logger.info(f"遅延通知の一括送信完了: {sent}/{len(queue)}件 成功")
        return sent
