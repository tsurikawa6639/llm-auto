"""ワード頻度分析モジュール

output/ideas/*.md のテキストデータ（タイトル・アイディア本文）から
形態素解析によりキーワード出現頻度を集計し、動画投稿日ごとの CSV として出力する。
"""

import argparse
import csv
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from janome.tokenizer import Tokenizer

logger = logging.getLogger(__name__)

# 日本標準時 (JST = UTC+9)
JST = timezone(timedelta(hours=9))

# プロジェクトルート（src/ の1つ上）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
TREND_DIR = OUTPUT_DIR / "trend"

# janome トークナイザ（モジュール読み込み時に1回だけ初期化）
_tokenizer = Tokenizer()

# --- フィルタ設定 ---
# 対象品詞: 名詞（一般・固有名詞・サ変接続）、形容詞
_TARGET_POS = {"名詞", "形容詞"}
# 除外する品詞細分類
_EXCLUDE_POS_DETAIL = {"非自立", "代名詞", "数", "接尾", "特殊"}
# 最小文字数（1文字の助詞的名詞を除外）
_MIN_WORD_LEN = 2
# 日本語ストップワード（よく出現するが分析に無意味な語）
_STOP_WORDS = {
    "こと", "もの", "ため", "それ", "これ", "ところ",
    "よう", "さん", "ここ", "そこ", "どこ",
    "とき", "なか", "うち", "ほか", "あと",
    "つもり", "はず", "わけ", "まま", "とおり",
    "動画", "投資", "株式", "アイディア", "投稿",  # ドメイン固有の一般語
    "要約", "チャンネル", "解説", "紹介", "情報",
    "以下", "以上", "今後", "現在", "個所",
    "根拠", "因果", "関係", "ステップ", "出力",
    "データ", "ソース", "判定",
    "YouTube", "公開", "具体", "特定", "全体",
    "投資家", "可能", "必要", "場合", "対象",
    "内容", "記述", "方法", "状況", "結果",
}
# Markdown記号を除去する前処理パターン
_MD_CLEANUP_RE = re.compile(r"^#{1,6}\s|^>\s|^\*\*.*?\*\*:?|^-\s|^\d+\.\s", re.MULTILINE)
# 日本語またはアルファベットを含むトークンのみ対象（記号のみを除外）
_HAS_WORD_CHAR_RE = re.compile(r"[\u3040-\u9FFFA-Za-z]")


def _parse_idea_md(filepath: Path) -> dict:
    """ideas/*.md から YAML frontmatter とアイディア本文を抽出する

    Returns:
        {"title": str, "published_at": str, "body": str}
    """
    text = filepath.read_text(encoding="utf-8")

    title = ""
    published_at = ""
    body = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            body = parts[2].strip()
            for line in frontmatter.splitlines():
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("published_at:"):
                    published_at = line.split(":", 1)[1].strip().strip('"')

    return {"title": title, "published_at": published_at, "body": body}


def _extract_published_date(published_at: str) -> str:
    """published_at 文字列から日付 (YYYYMMDD) を抽出する

    対応フォーマット:
        - "2026-03-15 20:07:34 JST"
        - "2026-03-15T11:07:34Z"
    """
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", published_at)
    if match:
        return f"{match.group(1)}{match.group(2)}{match.group(3)}"
    return ""


def _clean_markdown(text: str) -> str:
    """Markdown記号を前処理で除去する"""
    return _MD_CLEANUP_RE.sub("", text)


def _tokenize(text: str) -> list[str]:
    """janome で日本語テキストを形態素解析し、対象ワードを返す"""
    text = _clean_markdown(text)
    words = []
    for token in _tokenizer.tokenize(text):
        pos_parts = token.part_of_speech.split(",")
        pos_major = pos_parts[0]
        pos_detail = pos_parts[1] if len(pos_parts) > 1 else ""

        if pos_major not in _TARGET_POS:
            continue
        if pos_detail in _EXCLUDE_POS_DETAIL:
            continue

        surface = token.surface
        if len(surface) < _MIN_WORD_LEN:
            continue
        if surface in _STOP_WORDS:
            continue
        # 記号のみのトークンを除外（日本語・英字を含むものは許可）
        if not _HAS_WORD_CHAR_RE.search(surface):
            continue

        words.append(surface)
    return words


def collect_texts_by_date(days: int | None = None) -> dict[str, list[str]]:
    """ideas/*.md からテキストを動画投稿日ごとに収集する

    Args:
        days: 直近N日以内に作成されたファイルのみ対象（Noneで全件）

    Returns:
        {"20260315": ["テキスト1", "テキスト2", ...], ...}
    """
    texts_by_date: dict[str, list[str]] = defaultdict(list)

    ideas_dir = OUTPUT_DIR / "ideas"
    if not ideas_dir.exists():
        return texts_by_date

    # ファイル名の日付（抽出日）で絞り込む
    cutoff_date = None
    if days is not None:
        cutoff_date = (datetime.now(JST) - timedelta(days=days)).strftime("%Y%m%d")

    for md_file in sorted(ideas_dir.glob("*.md")):
        # 抽出日による絞り込み（古いファイルはスキップ）
        if cutoff_date:
            file_date = re.match(r"(\d{8})", md_file.name)
            if file_date and file_date.group(1) < cutoff_date:
                continue

        parsed = _parse_idea_md(md_file)

        # 投稿日を取得（取得できない場合はファイル名から推定）
        pub_date = _extract_published_date(parsed["published_at"])
        if not pub_date:
            match = re.match(r"(\d{8})", md_file.name)
            pub_date = match.group(1) if match else ""
        if not pub_date:
            continue

        # タイトルと本文を投稿日にグルーピング
        if parsed["title"]:
            texts_by_date[pub_date].append(parsed["title"])
        if parsed["body"]:
            texts_by_date[pub_date].append(parsed["body"])

    return dict(texts_by_date)


def analyze_and_save(top_n: int = 100, days: int | None = None) -> list[Path]:
    """データを投稿日ごとに分析し、日別CSVを出力する

    Args:
        top_n: 上位N件のキーワードを出力
        days: 直近N日以内のファイルのみ対象（Noneで全件）

    Returns:
        保存したCSVファイルパスのリスト
    """
    texts_by_date = collect_texts_by_date(days=days)

    if not texts_by_date:
        logger.warning("分析対象のテキストが見つかりません")
        return []

    logger.info(f"分析対象: {len(texts_by_date)}日分, "
                f"合計{sum(len(v) for v in texts_by_date.values())}テキスト")

    TREND_DIR.mkdir(parents=True, exist_ok=True)
    saved_files = []

    for date_str in sorted(texts_by_date.keys()):
        texts = texts_by_date[date_str]
        word_counter = Counter()

        for text in texts:
            words = _tokenize(text)
            word_counter.update(words)

        if not word_counter:
            continue

        # CSV出力
        csv_path = TREND_DIR / f"word_freq_{date_str}.csv"
        fieldnames = ["rank", "word", "count"]
        rows = []
        for rank, (word, count) in enumerate(word_counter.most_common(top_n), 1):
            rows.append({"rank": rank, "word": word, "count": count})

        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        saved_files.append(csv_path)

        # 日付ごとのサマリーログ
        formatted = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        top3 = word_counter.most_common(3)
        top3_str = ", ".join(f"{w}({c})" for w, c in top3)
        logger.info(f"  {formatted}: {len(texts)}件 → {csv_path.name}  TOP3: {top3_str}")

    return saved_files


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.Formatter.converter = lambda *args: datetime.now(JST).timetuple()

    parser = argparse.ArgumentParser(description="YouTube監視データ ワード頻度分析")
    parser.add_argument(
        "--top",
        type=int,
        default=200,
        help="上位N件のキーワードを出力（デフォルト: 200）",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="直近N日以内のファイルのみ再集計（省略時は全件）",
    )
    args = parser.parse_args()

    scope = f"直近{args.days}日分" if args.days else "全期間"
    logger.info(f"=== ワード頻度分析開始（投稿日別 / {scope}） ===")

    saved_files = analyze_and_save(top_n=args.top, days=args.days)

    if saved_files:
        logger.info(f"=== 完了: {len(saved_files)}日分のCSVを出力 ===")
    else:
        logger.warning("分析結果が空です")


if __name__ == "__main__":
    main()
