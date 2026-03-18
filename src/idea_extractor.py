"""Gemini APIによる投資アイディア抽出モジュール

YouTube動画のURLを直接Gemini APIに渡して、動画内容の要約と
投資アイディアの抽出を行う。
"""

import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# 日本標準時 (JST = UTC+9)
JST = timezone(timedelta(hours=9))


def _to_jst(utc_str: str) -> str:
    """UTC文字列 (例: '2026-03-14T11:00:01Z') をJST文字列に変換する"""
    if not utc_str:
        return "不明"
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    except (ValueError, AttributeError):
        return utc_str


def _format_duration(iso_duration: str) -> str:
    """ISO 8601 duration (例: 'PT10M30S') を '10分30秒' 形式に変換する"""
    if not iso_duration:
        return "不明"
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration)
    if not match:
        return iso_duration
    h, m, s = match.groups()
    parts = []
    if h:
        parts.append(f"{h}時間")
    if m:
        parts.append(f"{m}分")
    if s:
        parts.append(f"{s}秒")
    return "".join(parts) if parts else "0秒"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = _PROJECT_ROOT / "output" / "ideas"

EXTRACTION_PROMPT = """\
投資アナリストとして、YouTube動画「{title}」({channel})の映像・音声・字幕を分析せよ。

出力形式（厳守）:
SUMMARY:
（動画内容を3〜5行で要約）

IDEAS:
（具体的な投資アイディアがあれば以下の形式で記述。なければ「NONE」のみ）

# アイディアタイトル
## データソース
YouTube - {channel}「{title}」
## 根拠となった個所
> 動画内の具体的な発言や情報を引用
## 投資アイディア
具体的なアイディアの説明

判定基準: 投資に無関係、または具体性のない一般論のみ → IDEAS: NONE
"""



class IdeaExtractor:
    """Gemini APIでYouTube動画から直接投資アイディアを抽出する"""

    def __init__(self, api_key: str, models: list[str] | str = "gemini-3.1-flash-lite-preview", temperature: float = 0.3):
        self.client = genai.Client(api_key=api_key)
        # 後方互換: 文字列が渡された場合はリストに変換
        if isinstance(models, str):
            self.models = [models]
        else:
            self.models = list(models)
        self.temperature = temperature

    def extract_ideas(self, video_id: str, video_info: dict) -> tuple[str | None, str | None]:
        """YouTube動画URLを直接Geminiに渡して要約とアイディアを抽出する

        動画URLで処理できない場合はスキップする（None, None を返す）。

        Args:
            video_id: YouTube動画ID
            video_info: 動画情報（title, channel, published_at, description）

        Returns:
            (summary, idea_text | None) のタプル。
            summary: 動画内容の要約（処理失敗時は None）
            idea_text: 抽出されたアイディア（投資に無関係な場合は None）
        """
        title = video_info.get("title", "不明")
        channel = video_info.get("channel", "不明")

        # YouTube URLを直接渡して処理を試みる
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        prompt_text = EXTRACTION_PROMPT.format(
            title=title,
            channel=channel,
        )

        contents = types.Content(
            parts=[
                types.Part(
                    file_data=types.FileData(file_uri=video_url)
                ),
                types.Part(text=prompt_text),
            ]
        )

        # メインモデルで試行 → TPMエラー時はサブモデルにフォールバック
        for i, model in enumerate(self.models):
            is_fallback = i > 0
            try:
                if is_fallback:
                    logger.info(f"🔄 フォールバック: {model} で再試行")
                else:
                    logger.info(f"動画URLを直接Geminiに送信 [{model}]: {video_url}")

                response = self.client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(temperature=self.temperature),
                )
                result = response.text.strip()
                summary, idea_text = self._parse_response(result, video_info)
                logger.info(f"動画URL直接処理: 成功 [{model}]")
                return (summary, idea_text)

            except Exception as e:
                error_str = str(e)
                is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str

                if is_rate_limit and i < len(self.models) - 1:
                    # TPM/レート制限エラー → 次のモデルにフォールバック
                    logger.warning(f"⚠️ レート制限ヒット [{model}]: {e}")
                    continue
                else:
                    # 最後のモデルでもエラー or レート制限以外のエラー
                    logger.warning(f"⏭️ 動画URLの処理に失敗（スキップ）: {title} | エラー: {e}")
                    return (None, None)

        # ここには到達しないはずだが安全のため
        return (None, None)


    @staticmethod
    def _parse_response(result: str, video_info: dict) -> tuple[str, str | None]:
        """Gemini のレスポンスを SUMMARY と IDEAS に分割する"""
        summary = ""
        idea_text = None

        # SUMMARY: セクションを抽出
        summary_match = re.search(r"SUMMARY:\s*\n(.*?)(?=\nIDEAS:|$)", result, re.DOTALL)
        if summary_match:
            summary = summary_match.group(1).strip()

        # IDEAS: セクションを抽出
        ideas_match = re.search(r"IDEAS:\s*(.*)$", result, re.DOTALL)
        if ideas_match:
            ideas_content = ideas_match.group(1).strip()
            if (not ideas_content
                    or "NONE" in ideas_content.upper()
                    or "なし" in ideas_content):
                logger.info(f"投資アイディアなしと判定: {video_info.get('title', '不明')}")
                idea_text = None
            else:
                idea_text = ideas_content
        else:
            # パースできなかった場合はfallback
            logger.warning(f"レスポンスのパースに失敗。全文をアイディアとして扱います。")
            summary = summary or result[:200]
            idea_text = result

        return (summary, idea_text)

    def save_idea(self, video_id: str, video_info: dict, idea_text: str) -> Path:
        """抽出したアイディアをMarkdownファイルに保存する

        Returns:
            保存先ファイルパス
        """
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # 同じ video_id のファイルが既に存在する場合はスキップ（重複防止）
        existing = list(OUTPUT_DIR.glob(f"*_{video_id}.md"))
        if existing:
            logger.info(f"アイディアファイル既存のためスキップ: {existing[0].name}")
            return existing[0]

        # ファイル名: 日時_動画ID.md
        timestamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{video_id}.md"
        filepath = OUTPUT_DIR / filename

        # メタデータヘッダーを追加
        duration_str = _format_duration(video_info.get("duration", ""))
        view_count = video_info.get("view_count", "不明")
        header = f"""\
---
video_id: {video_id}
title: "{video_info.get('title', '不明')}"
channel: "{video_info.get('channel', '不明')}"
published_at: "{_to_jst(video_info.get('published_at', ''))}"
duration: "{duration_str}"
view_count: {view_count}
url: "https://www.youtube.com/watch?v={video_id}"
extracted_at: "{datetime.now(JST).isoformat()}"
---

"""
        filepath.write_text(header + idea_text, encoding="utf-8")
        logger.info(f"アイディア保存: {filepath}")
        return filepath
