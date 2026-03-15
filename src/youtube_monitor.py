"""YouTube検索＆動画情報取得モジュール"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEEN_VIDEOS_PATH = _PROJECT_ROOT / "data" / "seen_videos.json"
PENDING_VIDEOS_PATH = _PROJECT_ROOT / "data" / "pending_videos.json"


class YouTubeMonitor:
    """YouTube Data API v3 を使って投資関連の新着動画を検索する"""

    def __init__(self, api_key: str, max_results: int = 10, published_after_hours: int = 2):
        self.youtube = build("youtube", "v3", developerKey=api_key)
        self.max_results = max_results
        self.published_after_hours = published_after_hours
        self._seen_videos = self._load_seen_videos()

    def _load_seen_videos(self) -> set[str]:
        """処理済み動画IDをファイルから読み込む"""
        if SEEN_VIDEOS_PATH.exists():
            try:
                data = json.loads(SEEN_VIDEOS_PATH.read_text(encoding="utf-8"))
                return set(data)
            except (json.JSONDecodeError, ValueError):
                logger.warning("seen_videos.json の読み込みに失敗。新規作成します。")
        return set()

    def _save_seen_videos(self) -> None:
        """処理済み動画IDをファイルに保存する"""
        SEEN_VIDEOS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SEEN_VIDEOS_PATH.write_text(
            json.dumps(sorted(self._seen_videos), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def is_already_processed(self, video_id: str) -> bool:
        return video_id in self._seen_videos

    def mark_as_processed(self, video_id: str) -> None:
        self._seen_videos.add(video_id)
        self._save_seen_videos()

    # --- 保留キュー管理 ---

    @staticmethod
    def load_pending_videos() -> list[dict]:
        """前回未処理の保留動画をファイルから読み込む"""
        if PENDING_VIDEOS_PATH.exists():
            try:
                data = json.loads(PENDING_VIDEOS_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    logger.info(f"保留キューから {len(data)} 件の動画を読み込み")
                    return data
            except (json.JSONDecodeError, ValueError):
                logger.warning("pending_videos.json の読み込みに失敗。空として扱います。")
        return []

    @staticmethod
    def save_pending_videos(videos: list[dict]) -> None:
        """未処理動画を保留キューとしてファイルに保存する"""
        PENDING_VIDEOS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_VIDEOS_PATH.write_text(
            json.dumps(videos, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if videos:
            logger.info(f"保留キューに {len(videos)} 件の動画を保存（次回優先処理）")
        else:
            logger.info("保留キューをクリア")

    @staticmethod
    def clear_pending_videos() -> None:
        """保留キューをクリアする"""
        if PENDING_VIDEOS_PATH.exists():
            PENDING_VIDEOS_PATH.unlink()
            logger.debug("pending_videos.json を削除")

    def search_recent_videos(self, keyword: str, exclude_ids: set[str] | None = None) -> list[dict]:
        """キーワードで直近N時間の新着動画を検索する

        videoDuration パラメータで medium（4〜20分）と long（20分超）の2回検索を行い、
        ショート動画を検索段階で除外する。APIコストは 200ユニット/キーワード。

        Args:
            keyword: 検索キーワード
            exclude_ids: 除外する動画IDのセット（他キーワードで既に取得済みなど）

        Returns:
            動画情報のリスト [{"video_id": str, "title": str, "channel": str}]
        """
        published_after = datetime.now(timezone.utc) - timedelta(hours=self.published_after_hours)
        published_after_str = published_after.strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info(f"検索キーワード: '{keyword}' (直近{self.published_after_hours}時間)")

        # medium（4〜20分）と long（20分超）の2回検索でショート動画を除外
        all_items: list[dict] = []
        for duration in ["medium", "long"]:
            try:
                response = self.youtube.search().list(
                    q=keyword,
                    part="snippet",
                    type="video",
                    order="date",
                    publishedAfter=published_after_str,
                    relevanceLanguage="ja",
                    maxResults=self.max_results,
                    videoDuration=duration,
                ).execute()
                items = response.get("items", [])
                all_items.extend(items)
                logger.info(f"  videoDuration={duration}: {len(items)}件")
            except Exception as e:
                logger.error(f"YouTube検索でエラー (videoDuration={duration}): {e}")

        candidates = []
        skipped_duplicate = 0
        seen_in_batch: set[str] = set()  # medium/long 間の重複排除
        for item in all_items:
            video_id = item["id"]["videoId"]
            if video_id in seen_in_batch:
                continue
            seen_in_batch.add(video_id)
            if self.is_already_processed(video_id):
                logger.debug(f"スキップ（処理済み）: {video_id}")
                continue
            if exclude_ids and video_id in exclude_ids:
                skipped_duplicate += 1
                logger.debug(f"スキップ（他キーワードで取得済み）: {video_id}")
                continue

            candidates.append({
                "video_id": video_id,
                "title": item["snippet"]["title"],
                "channel": item["snippet"]["channelTitle"],
                "description": item["snippet"].get("description", ""),
                "published_at": item["snippet"]["publishedAt"],
            })

        if skipped_duplicate:
            logger.info(f"他キーワードとの重複排除: {skipped_duplicate}件スキップ")

        logger.info(
            f"新着動画: {len(candidates)}件"
            f"（検索結果: {len(all_items)}件）"
        )
        return candidates


    def get_video_details(self, video_ids: list[str]) -> dict[str, dict]:
        """動画の詳細情報（完全な概要欄など）をバッチ取得する"""
        if not video_ids:
            return {}

        try:
            response = self.youtube.videos().list(
                id=",".join(video_ids),
                part="snippet,contentDetails,statistics",
            ).execute()
        except Exception as e:
            logger.error(f"動画詳細の取得でエラー: {e}")
            return {}

        details = {}
        for item in response.get("items", []):
            vid = item["id"]
            stats = item.get("statistics", {})
            details[vid] = {
                "title": item["snippet"]["title"],
                "channel": item["snippet"]["channelTitle"],
                "description": item["snippet"].get("description", ""),
                "published_at": item["snippet"]["publishedAt"],
                "duration": item["contentDetails"].get("duration", ""),
                "view_count": stats.get("viewCount", ""),
            }
        return details
