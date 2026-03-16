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
あなたは投資アナリストです。以下のYouTube動画を分析してください。

## 動画情報
- タイトル: {title}
- チャンネル: {channel}
- 公開日: {published_at}

## 指示
動画の映像・音声・字幕をすべて参照して、内容を簡潔に要約し、
次に株式投資に活用できる具体的なアイディアがあるかどうかを判定してください。
必ず以下の出力形式に従ってください。

## 出力形式

### ステップ1: 要約（必須）
「SUMMARY:」の後に、動画内容を3〜5行で簡潔に要約してください。

### ステップ2: 投資アイディア判定
- 具体的な投資アイディアがある場合 → 「IDEAS:」の後にアイディアを記述
- 投資に無関係、または具体性のない一般論のみの場合 → 「IDEAS: NONE」と記述

## 出力例（アイディアがある場合）
SUMMARY:
動画では○○について解説しており、△△の業績が好調であること、□□の市場拡大が見込まれることなどが紹介されていた。

IDEAS:
# [アイディアのタイトル]

## データソース
YouTube - {channel}「{title}」
公開日: {published_at}

## 根拠となった個所
> [動画内の具体的な発言や情報を引用]

## 投資アイディア
[具体的なアイディアの説明]

## 因果関係
1. [因果の連鎖を numbered list で記述]

## 出力例（アイディアがない場合）
SUMMARY:
動画では投資の基本的な考え方について一般論が紹介されていた。

IDEAS: NONE
"""

# 概要欄フォールバック用プロンプト（動画URLを処理できなかった場合に使用）
FALLBACK_PROMPT = """\
あなたは投資アナリストです。以下のYouTube動画の情報を分析してください。

## 動画情報
- タイトル: {title}
- チャンネル: {channel}
- 公開日: {published_at}

## 動画の概要欄
{content}

## 注意
動画本体にはアクセスできなかったため、概要欄の情報のみで分析してください。
情報が限られているため、推測は控えめにしてください。

## 指示
まず動画の内容を簡潔に要約し、次に株式投資に活用できる具体的なアイディアがあるかどうかを判定してください。
必ず以下の出力形式に従ってください。

## 出力形式

### ステップ1: 要約（必須）
「SUMMARY:」の後に、動画内容を3〜5行で簡潔に要約してください。

### ステップ2: 投資アイディア判定
- 具体的な投資アイディアがある場合 → 「IDEAS:」の後にアイディアを記述
- 投資に無関係、または具体性のない一般論のみの場合 → 「IDEAS: NONE」と記述

## 出力例（アイディアがある場合）
SUMMARY:
動画では○○について解説しており、△△の業績が好調であること、□□の市場拡大が見込まれることなどが紹介されていた。

IDEAS:
# [アイディアのタイトル]

## データソース
YouTube - {channel}「{title}」
公開日: {published_at}

## 根拠となった個所
> [動画内の具体的な発言や情報を引用]

## 投資アイディア
[具体的なアイディアの説明]

## 因果関係
1. [因果の連鎖を numbered list で記述]

## 出力例（アイディアがない場合）
SUMMARY:
動画では投資の基本的な考え方について一般論が紹介されていた。

IDEAS: NONE
"""


class IdeaExtractor:
    """Gemini APIでYouTube動画から直接投資アイディアを抽出する"""

    def __init__(self, api_key: str, model: str = "gemini-3.1-flash-lite-preview", temperature: float = 0.3):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.temperature = temperature

    def extract_ideas(self, video_id: str, video_info: dict) -> tuple[str, str | None]:
        """YouTube動画URLを直接Geminiに渡して要約とアイディアを抽出する

        動画URLで処理できない場合は概要欄テキストにフォールバックする。

        Args:
            video_id: YouTube動画ID
            video_info: 動画情報（title, channel, published_at, description）

        Returns:
            (summary, idea_text | None) のタプル。
            summary: 動画内容の要約（常に返す）
            idea_text: 抽出されたアイディア（投資に無関係な場合は None）
        """
        title = video_info.get("title", "不明")
        channel = video_info.get("channel", "不明")
        published_at = video_info.get("published_at", "不明")

        # まずYouTube URLを直接渡して処理を試みる
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        prompt_text = EXTRACTION_PROMPT.format(
            title=title,
            channel=channel,
            published_at=published_at,
        )

        try:
            logger.info(f"動画URLを直接Geminiに送信: {video_url}")
            response = self.client.models.generate_content(
                model=self.model,
                contents=types.Content(
                    parts=[
                        types.Part(
                            file_data=types.FileData(file_uri=video_url)
                        ),
                        types.Part(text=prompt_text),
                    ]
                ),
                config=types.GenerateContentConfig(temperature=self.temperature),
            )
            result = response.text.strip()
            summary, idea_text = self._parse_response(result, video_info)
            logger.info("動画URL直接処理: 成功")
            return (summary, idea_text)

        except Exception as e:
            logger.warning(f"動画URLの直接処理に失敗: {e}")
            logger.info("概要欄テキストへフォールバック")

        # フォールバック: 概要欄テキストで処理
        return self._extract_from_description(video_info)

    def _extract_from_description(self, video_info: dict) -> tuple[str, str | None]:
        """概要欄テキストを使って要約・アイディア抽出する（フォールバック）"""
        description = video_info.get("description", "")

        if not description or len(description.strip()) < 50:
            logger.info(f"概要欄も短すぎるためスキップ: {video_info.get('title', '不明')}")
            return ("動画にアクセスできず、概要欄の情報も不十分なため要約不可", None)

        prompt = FALLBACK_PROMPT.format(
            title=video_info.get("title", "不明"),
            channel=video_info.get("channel", "不明"),
            published_at=video_info.get("published_at", "不明"),
            content=description[:8000],
        )

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=types.Content(
                    parts=[
                        types.Part(text=prompt),
                    ]
                ),
                config=types.GenerateContentConfig(temperature=self.temperature),
            )
            result = response.text.strip()
            logger.info("概要欄フォールバック処理: 成功")
        except Exception as e:
            logger.error(f"Gemini API呼び出しエラー（フォールバック）: {e}")
            return ("API呼び出しエラー", None)

        summary, idea_text = self._parse_response(result, video_info)
        return (summary, idea_text)

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
            if (ideas_content.upper() == "NONE"
                    or not ideas_content
                    or "該当なし" in ideas_content):
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
