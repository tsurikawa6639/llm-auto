"""YouTube定刻監視システム — エントリーポイント"""

import argparse
try:
    import fcntl
except ImportError:
    fcntl = None  # Windows/CI環境ではロック機能を無効化
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from youtube_monitor import YouTubeMonitor
from idea_extractor import IdeaExtractor
from discord_notifier import DiscordNotifier

# 日本標準時 (JST = UTC+9)
JST = timezone(timedelta(hours=9))

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# ログのタイムスタンプをJSTにする
logging.Formatter.converter = lambda *args: datetime.now(JST).timetuple()
logger = logging.getLogger(__name__)

# .env ファイルから環境変数を読み込む
load_dotenv()


# プロジェクトルート（src/ の1つ上）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    """config.yaml を読み込む"""
    config_path = PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        logger.error("config.yaml が見つかりません")
        sys.exit(1)
    return yaml.safe_load(config_path.read_text(encoding="utf-8"))


LOCK_FILE_PATH = Path("/tmp/run_monitor.lock")


def run_monitor(keywords: list[str], config: dict) -> None:
    """1回分の監視サイクルを実行する

    処理フロー:
    1. ロックファイルで多重起動を防止
    2. 前回の保留キューから未処理動画を読み込む（優先）
    3. 全キーワードでYouTube検索し、新着動画を収集
    4. 保留キュー + 新着を結合し、先頭N件のみGemini APIで処理
    5. 残りを保留キューに保存（次回実行で優先処理）
    """
    # --- ロックファイルで多重起動を防止 ---
    if fcntl is None:
        # fcntlが使えない環境（Windows/CI）ではロックをスキップ
        try:
            _run_monitor_impl(keywords, config)
        except Exception:
            logger.exception("監視サイクルでエラーが発生")
        return

    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.warning("別プロセスが実行中のためスキップします（ロック取得失敗）")
        lock_fd.close()
        return

    try:
        _run_monitor_impl(keywords, config)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _run_monitor_impl(keywords: list[str], config: dict) -> None:
    """監視サイクルの実処理（ロック取得後に呼ばれる）"""
    youtube_api_key = os.getenv("YOUTUBE_API_KEY")
    gemini_api_key = os.getenv("GEMINI_API_KEY")

    if not youtube_api_key or youtube_api_key == "your_youtube_api_key_here":
        logger.error("YOUTUBE_API_KEY が設定されていません。.env ファイルを確認してください。")
        return
    if not gemini_api_key or gemini_api_key == "your_gemini_api_key_here":
        logger.error("GEMINI_API_KEY が設定されていません。.env ファイルを確認してください。")
        return

    youtube_config = config.get("youtube", {})
    gemini_config = config.get("gemini", {})
    discord_config = config.get("discord", {})
    max_gemini_requests = gemini_config.get("max_requests_per_run", 10)

    # 各モジュールの初期化
    monitor = YouTubeMonitor(
        api_key=youtube_api_key,
        max_results=youtube_config.get("max_results_per_search", 10),
        published_after_hours=youtube_config.get("published_after_hours", 2),
        channel_blacklist=youtube_config.get("channel_blacklist", []),
    )
    extractor = IdeaExtractor(
        api_key=gemini_api_key,
        model=gemini_config.get("model", "gemini-3.1-flash-lite-preview"),
        temperature=gemini_config.get("temperature", 0.3),
    )

    # Discord通知の初期化（enabled=false または URL未設定なら送信スキップ）
    discord_enabled = discord_config.get("enabled", True)
    if discord_enabled:
        notifier = DiscordNotifier(
            summary_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
            idea_webhook_url=os.getenv("DISCORD_IDEA_WEBHOOK_URL", ""),
        )
    else:
        notifier = None

    # --- Phase 1: 保留キューの動画を読み込み ---
    pending_videos = monitor.load_pending_videos()

    # --- Phase 2: 全キーワードで検索し、新着動画を収集 ---
    new_videos: list[dict] = []
    seen_ids = {v["video_id"] for v in pending_videos}  # 重複排除用

    for keyword in keywords:
        logger.info(f"=== キーワード: '{keyword}' ===")
        videos = monitor.search_recent_videos(keyword, exclude_ids=seen_ids)
        if not videos:
            logger.info("新着動画なし")
            continue
        for video in videos:
            if video["video_id"] not in seen_ids:
                video["keyword"] = keyword  # どのキーワードで見つかったか記録
                new_videos.append(video)
                seen_ids.add(video["video_id"])

    # --- Phase 3: 保留キュー（優先）＋ 新着を結合 ---
    all_videos = pending_videos + new_videos

    # ブラックリストによるフィルタリング（保留キューにも適用）
    if monitor._channel_blacklist or monitor._blacklist_channel_names:
        before_count = len(all_videos)

        def _is_blacklisted(v: dict) -> bool:
            """channel_id があればIDで、なければチャンネル名でフィルタ"""
            ch_id = v.get("channel_id", "")
            if ch_id and ch_id in monitor._channel_blacklist:
                return True
            # channel_id が欠落している場合はチャンネル名でフォールバック
            if not ch_id and monitor._blacklist_channel_names:
                ch_name = v.get("channel", "")
                if ch_name in monitor._blacklist_channel_names:
                    return True
            return False

        all_videos = [v for v in all_videos if not _is_blacklisted(v)]
        filtered = before_count - len(all_videos)
        if filtered:
            logger.info(f"⛔ ブラックリストにより {filtered}件を除外")

    logger.info(
        f"処理対象: 保留={len(pending_videos)}件 + 新着={len(new_videos)}件 "
        f"= 合計{len(all_videos)}件 (上限{max_gemini_requests}件)"
    )

    # 上限分割: 処理対象と保留
    to_process = all_videos[:max_gemini_requests]
    to_pending = all_videos[max_gemini_requests:]

    # --- Phase 3.5: 動画の詳細情報（尺・概要欄など）をバッチ取得 ---
    process_ids = [v["video_id"] for v in to_process]
    video_details = monitor.get_video_details(process_ids)
    for video in to_process:
        detail = video_details.get(video["video_id"], {})
        video["duration"] = detail.get("duration", "")
        video["view_count"] = detail.get("view_count", "")
        # 詳細APIから完全な概要欄を取得（検索APIの概要欄は切り詰められている）
        if detail.get("description"):
            video["description"] = detail["description"]

    # --- Phase 4: Gemini APIで処理（上限件数まで） ---
    total_ideas = 0
    results: list[dict] = []

    for video in to_process:
        video_id = video["video_id"]
        title = video["title"]
        url = f"https://www.youtube.com/watch?v={video_id}"
        channel = video.get("channel", "不明")
        keyword = video.get("keyword", "不明")
        logger.info(f"処理中: [{video_id}] {title}")

        # YouTube動画URLを直接Geminiに渡して要約＋アイディア抽出
        summary, idea_text = extractor.extract_ideas(video_id, video)

        # 動画URL処理失敗 → スキップ
        if summary is None:
            logger.warning(f"⏭️ スキップ（動画処理失敗）: {title}")
            if notifier:
                notifier.send_skip(video)
            results.append({
                "keyword": keyword,
                "video_id": video_id,
                "title": title,
                "channel": channel,
                "url": url,
                "summary": "動画処理失敗のためスキップ",
                "idea": "⏭️ スキップ",
            })
            monitor.mark_as_processed(video_id)
            continue

        logger.info(f"要約: {summary[:80]}..." if len(summary) > 80 else f"要約: {summary}")

        if idea_text:
            filepath = extractor.save_idea(video_id, video, idea_text)
            total_ideas += 1
            logger.info(f"✅ アイディア抽出成功 → {filepath.name}")
            idea_status = f"✅ あり → {filepath.name}"
            # Discord アイデアチャンネルに個別送信
            if notifier:
                notifier.send_idea(video, summary, idea_text)
        else:
            logger.info(f"⏭️ 投資アイディアなし: {title}")
            idea_status = "❌ なし"

        results.append({
            "keyword": keyword,
            "video_id": video_id,
            "title": title,
            "channel": channel,
            "url": url,
            "summary": summary,
            "idea": idea_status,
        })

        # 処理済みとしてマーク
        monitor.mark_as_processed(video_id)

        # API負荷軽減のため動画間でスリープ
        logger.info("次の動画まで50秒待機...")
        time.sleep(50)

    # --- Phase 5: 保留キュー保存 ---
    monitor.save_pending_videos(to_pending)

    logger.info(
        f"=== 完了: 処理={len(to_process)}件 → アイディア{total_ideas}件抽出 "
        f"| 保留={len(to_pending)}件 ==="
    )

    # サマリーレポート出力
    if results:
        save_summary_report(results, len(to_process), total_ideas)
        # Discord サマリーチャンネルに送信
        if notifier:
            notifier.send_summary(results, len(to_process), total_ideas)


def save_summary_report(results: list[dict], total_new: int, total_ideas: int) -> None:
    """調査結果のサマリーレポートをCSVとMarkdownで出力する"""
    import csv

    csv_dir = PROJECT_ROOT / "output" / "csv"
    md_dir = PROJECT_ROOT / "output" / "md"
    csv_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")

    # --- CSV出力 ---
    csv_path = csv_dir / f"report_{timestamp}.csv"
    csv_fieldnames = ["keyword", "title", "channel", "url", "idea"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow({k: v for k, v in row.items() if k in csv_fieldnames})
    logger.info(f"📊 CSVレポート保存: {csv_path}")

    # --- Markdown出力 ---
    md_path = md_dir / f"report_{timestamp}.md"
    lines = [
        f"# YouTube定刻監視レポート",
        f"",
        f"- **実行日時**: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **調査動画数**: {total_new}件",
        f"- **アイディア抽出数**: {total_ideas}件",
        f"",
        f"## 調査結果一覧",
        f"",
        f"| # | タイトル | チャンネル | アイディア |",
        f"|---|---------|-----------|-----------| ",
    ]
    for i, row in enumerate(results, 1):
        title_link = f"[{row['title']}]({row['url']})"
        lines.append(f"| {i} | {title_link} | {row['channel']} | {row['idea']} |")

    # --- 各動画の詳細（要約） ---
    lines.append("")
    lines.append("## 動画詳細")
    lines.append("")
    for i, row in enumerate(results, 1):
        summary = row.get("summary", "ー")
        lines.append(f"### {i}. {row['title']}")
        lines.append(f"- **チャンネル**: {row['channel']}")
        lines.append(f"- **URL**: {row['url']}")
        lines.append(f"- **アイディア**: {row['idea']}")
        lines.append(f"")
        lines.append(f"**要約**:")
        lines.append(f"{summary}")
        lines.append(f"")
        lines.append("---")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"📝 Markdownレポート保存: {md_path}")


def main():
    parser = argparse.ArgumentParser(description="YouTube定刻監視 — 投資アイディア自動抽出")
    parser.add_argument(
        "--once",
        action="store_true",
        help="1回だけ実行して終了（スケジューラを使わない）",
    )
    parser.add_argument(
        "-k", "--keyword",
        action="append",
        dest="keywords",
        help="検索キーワード（複数指定可: -k 'キーワード1' -k 'キーワード2'）",
    )
    args = parser.parse_args()

    config = load_config()

    # CLI引数のキーワードがあればそちらを優先、なければconfig.yamlのデフォルト値
    keywords = args.keywords or config.get("youtube", {}).get("search_keywords", [])

    if not keywords:
        logger.error("検索キーワードが指定されていません。-k オプションまたは config.yaml で設定してください。")
        sys.exit(1)

    logger.info(f"検索キーワード: {keywords}")

    if args.once:
        # 1回実行モード
        logger.info("--- 1回実行モード ---")
        run_monitor(keywords, config)
    else:
        # スケジューラモード
        interval_hours = config.get("schedule", {}).get("interval_hours", 1)
        logger.info(f"--- スケジューラモード（{interval_hours}時間間隔） ---")

        # 起動直後に1回実行
        run_monitor(keywords, config)

        scheduler = BlockingScheduler()
        scheduler.add_job(
            run_monitor,
            "interval",
            hours=interval_hours,
            args=[keywords, config],
            id="youtube_monitor",
        )

        try:
            logger.info("スケジューラ開始。Ctrl+C で停止。")
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("スケジューラ停止。")


if __name__ == "__main__":
    main()
