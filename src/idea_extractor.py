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
あなたは厳格な投資アナリストです。YouTube動画「{title}」({channel}) の
映像・音声・字幕を分析し、**動画内で実際に語られた内容のみ**に基づいて
投資に役立つ知見を抽出してください。

## 絶対遵守ルール
1. 推測・一般知識による補完は禁止。動画で言及されていない情報は出力しない。
2. 根拠は必ず動画内の発言を**逐語引用**し、可能ならタイムスタンプ(MM:SS)を付ける。
3. スポンサー枠/PR/案件紹介の区間は除外する。
4. 過去の振り返り・自慢話のみで、今後の判断に使えないものは採用しない。
5. 「分散投資が大事」「長期目線で」等、**特定の状況・対象・条件**が無い精神論は採用しない。

## 抽出する3カテゴリ
- **[A] 個別アイディア** — 特定の銘柄/資産に対する売買・監視判断
- **[B] 戦略ルール** — 「こういう相場ではこう動く」という条件付きの行動指針
- **[C] 市場観察** — 銘柄/資産/指標間の相関・連動・リード&ラグなどの関係性

## 出力形式（厳守）

冒頭に「IDEAS:」と記載し、その下に採用基準を満たすエントリを下記いずれかの型で列挙する。
1つも無ければ「IDEAS: NONE」のみ出力。

- 各エントリの先頭は必ず `# [A]` / `# [B]` / `# [C]` のいずれかで始める。
- **下記の「型定義」自体（例: 「型A: 個別アイディア」のような見出し）は出力に含めない**。
- 信頼度は「高」「中」「低」のいずれか一語のみ。説明文や注釈は付けない。
- 各 `##` 見出しは下記のものを**完全一致で**使用する（括弧内の補足は出力に含めない）。

---

【型A: 個別アイディア】

# [A] {{銘柄名 or 対象資産}} ({{ティッカー/コード, 不明なら「不明」}})
## アクション
{{買い / 売り(空売り) / 監視 / 回避}}
## 時間軸
{{短期(〜3ヶ月) / 中期(3〜12ヶ月) / 長期(1年以上) / 不明}}
## 触媒
{{価格を動かす想定イベントや要因。動画で言及されたもののみ}}
## 根拠となった発言
> 「逐語引用」 ({{MM:SS}})
## 主要リスク
{{動画内で言及されたリスク。無ければ「動画内では言及なし」}}
## 信頼度
{{高 / 中 / 低}}

---

【型B: 戦略ルール】

# [B] {{戦略の短い名前(例: 「逆イールド時のディフェンシブ移行」)}}
## 適用条件
{{発動条件を具体的に。例:「VIXが30超」「FRB利下げ局面」「決算プレ前」など}}
## 推奨アクション
{{条件が満たされた時に取るべき行動。資産配分の変更、ヘッジ、避けるべき行動など}}
## ロジック・根拠
{{なぜそれが有効と語られたか。歴史的事例があれば併記}}
## 根拠となった発言
> 「逐語引用」 ({{MM:SS}})
## 前提・効かなくなる条件
{{戦略が機能しなくなる前提崩れのシナリオ。言及無しなら「動画内では言及なし」}}
## 信頼度
{{高 / 中 / 低}}

---

【型C: 市場観察】

# [C] {{対象A}} × {{対象B}}（{{関係性タイプ}}）
## 関係性タイプ
{{正相関 / 負相関(逆相関) / リード&ラグ / 連動崩れ / レジーム依存 など}}
## 関係の内容
{{どう動くと何がどう動くのか。タイムラグや強度の言及があれば含める}}
## 観察された期間・条件
{{動画で言及された具体的な期間や前提。無ければ「動画内では明示なし」}}
## 根拠となった発言
> 「逐語引用」 ({{MM:SS}})
## 投資への活かし方
{{この関係性をどう取引に活かせると語られたか}}
## 信頼度
{{高 / 中 / 低}}

---

データソース（全エントリ共通）: YouTube - {channel}「{title}」
"""



class IdeaExtractor:
    """Gemini APIでYouTube動画から直接投資アイディアを抽出する"""

    def __init__(self, api_key: str, models: list[str] | str = "gemini-3.1-flash-lite", temperature: float = 0.3):
        self.client = genai.Client(api_key=api_key)
        # 後方互換: 文字列が渡された場合はリストに変換
        if isinstance(models, str):
            self.models = [models]
        else:
            self.models = list(models)
        self.temperature = temperature

    def extract_ideas(self, video_id: str, video_info: dict) -> tuple[str, str | None]:
        """YouTube動画URLを直接Geminiに渡してアイディアを抽出する

        Args:
            video_id: YouTube動画ID
            video_info: 動画情報（title, channel, published_at, description）

        Returns:
            (status, idea_text) のタプル。
            status:
                - "ok"               : 処理成功（idea_text が None なら投資アイディアなし）
                - "skip_retryable"   : 503 UNAVAILABLE 等の一過性エラー。呼び出し側で後段リトライ可
                - "skip_permanent"   : 動画自体が処理不能などの永続的失敗
            idea_text: 抽出されたアイディア（投資に無関係 or 失敗時は None）
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

        # メインモデルで試行 → レート制限/過負荷エラー時はサブモデルにフォールバック
        last_error: Exception | None = None
        last_was_retryable = False
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
                _, idea_text = self._parse_response(result, video_info)
                logger.info(f"動画URL直接処理: 成功 [{model}]")
                return ("ok", idea_text)

            except Exception as e:
                error_str = str(e)
                is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
                is_overloaded = "503" in error_str or "UNAVAILABLE" in error_str
                last_error = e
                last_was_retryable = is_overloaded

                if (is_rate_limit or is_overloaded) and i < len(self.models) - 1:
                    # 過負荷/レート制限エラー → 次のモデルにフォールバック
                    logger.warning(f"⚠️ {model} で過負荷/レート制限: {e}")
                    continue
                else:
                    # 最後のモデルでもエラー or 永続的エラー
                    break

        # 全モデルで失敗
        if last_was_retryable:
            logger.warning(f"⏭️ 動画URL処理失敗（503/UNAVAILABLE、後でリトライ）: {title} | エラー: {last_error}")
            return ("skip_retryable", None)
        else:
            logger.warning(f"⏭️ 動画URLの処理に失敗（スキップ）: {title} | エラー: {last_error}")
            return ("skip_permanent", None)


    @staticmethod
    def _parse_response(result: str, video_info: dict) -> tuple[str, str | None]:
        """Gemini のレスポンスから IDEAS を抽出する

        SUMMARY セクションは廃止。後方互換のため戻り値は (summary, idea_text) のタプルを
        維持するが、summary は常に空文字を返す。
        """
        idea_text: str | None = None

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
            # IDEAS: ヘッダなしで本文が返るケース。全文を idea_text として扱う
            stripped = result.strip()
            if not stripped or "NONE" in stripped.upper():
                idea_text = None
            else:
                idea_text = stripped

        return ("", idea_text)

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
