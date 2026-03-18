"""DiscordNotifier のユニットテスト"""

from unittest.mock import patch, MagicMock

import pytest

# テスト対象のモジュールが src/ にあるため、パスを通す
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from discord_notifier import DiscordNotifier


# --- サマリー送信テスト ---

class TestSendSummary:
    """send_summary のテスト"""

    def setup_method(self):
        self.notifier = DiscordNotifier(
            summary_webhook_url="https://discord.com/api/webhooks/test/summary",
            idea_webhook_url="https://discord.com/api/webhooks/test/idea",
        )
        self.results = [
            {
                "keyword": "株",
                "video_id": "abc123",
                "title": "テスト動画1",
                "channel": "テストチャンネル",
                "url": "https://www.youtube.com/watch?v=abc123",
                "summary": "テスト要約",
                "idea": "✅ あり → 20260315_abc123.md",
            },
            {
                "keyword": "株",
                "video_id": "def456",
                "title": "テスト動画2",
                "channel": "テストチャンネル2",
                "url": "https://www.youtube.com/watch?v=def456",
                "summary": "テスト要約2",
                "idea": "❌ なし",
            },
        ]

    @patch("discord_notifier.requests.post")
    def test_send_summary_success(self, mock_post):
        """サマリー送信が成功する"""
        mock_post.return_value = MagicMock(status_code=204)
        result = self.notifier.send_summary(self.results, 2, 1)
        assert result is True
        mock_post.assert_called_once()
        # POSTされたペイロードを検証
        payload = mock_post.call_args[1]["json"]
        assert payload["embeds"][0]["title"] == "📊 YouTube監視レポート"
        assert "2件" in payload["embeds"][0]["description"]

    @patch("discord_notifier.requests.post")
    def test_send_summary_failure(self, mock_post):
        """APIエラー時に False を返す"""
        mock_post.return_value = MagicMock(status_code=400, text="Bad Request")
        result = self.notifier.send_summary(self.results, 2, 1)
        assert result is False

    def test_send_summary_no_url(self):
        """Webhook URL 未設定で送信がスキップされる"""
        notifier = DiscordNotifier(summary_webhook_url="", idea_webhook_url="")
        result = notifier.send_summary(self.results, 2, 1)
        assert result is False


# --- アイデア個別送信テスト ---

class TestSendIdea:
    """send_idea のテスト"""

    def setup_method(self):
        self.notifier = DiscordNotifier(
            summary_webhook_url="https://discord.com/api/webhooks/test/summary",
            idea_webhook_url="https://discord.com/api/webhooks/test/idea",
        )
        self.video_info = {
            "video_id": "abc123",
            "title": "半導体銘柄の分析",
            "channel": "投資チャンネル",
            "published_at": "2026-03-15T11:00:00Z",
            "view_count": "12345",
        }
        self.summary = "動画では半導体業界の最新動向について解説。"
        self.idea_text = (
            "# 半導体銘柄の投資チャンス\n\n"
            "## データソース\n"
            "YouTube - 投資チャンネル「半導体銘柄の分析」\n\n"
            "## 根拠となった個所\n"
            "> AI需要拡大により半導体需要が増加している\n\n"
            "## 投資アイディア\n"
            "国内半導体関連銘柄に投資チャンスがある。"
        )

    @patch("discord_notifier.requests.post")
    def test_send_idea_success(self, mock_post):
        """アイデア個別送信が成功する"""
        mock_post.return_value = MagicMock(status_code=204)
        result = self.notifier.send_idea(self.video_info, self.summary, self.idea_text)
        assert result is True
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        embed = payload["embeds"][0]
        assert "半導体銘柄の投資チャンス" in embed["title"]
        # フィールドが構築されていることを検証
        field_names = [f["name"] for f in embed["fields"]]
        assert "📺 データソース" in field_names
        assert "📝 要約" in field_names
        assert "🎯 投資アイディア" in field_names

    def test_send_idea_no_url(self):
        """Webhook URL 未設定で送信がスキップされる"""
        notifier = DiscordNotifier(summary_webhook_url="", idea_webhook_url="")
        result = notifier.send_idea(self.video_info, self.summary, self.idea_text)
        assert result is False


# --- ユーティリティテスト ---

class TestUtilities:
    """ユーティリティメソッドのテスト"""

    def test_extract_idea_title(self):
        """# で始まるタイトルを正しく抽出する"""
        text = "# 半導体銘柄の投資チャンス\n\n## データソース\n..."
        title = DiscordNotifier._extract_idea_title(text)
        assert title == "半導体銘柄の投資チャンス"

    def test_extract_idea_title_no_title(self):
        """タイトルがない場合はデフォルトを返す"""
        text = "## データソース\nテスト"
        title = DiscordNotifier._extract_idea_title(text)
        assert title == "投資アイデア"

    def test_parse_idea_sections(self):
        """Markdown セクションを正しく分割する"""
        text = (
            "# タイトル\n\n"
            "## データソース\nYouTube - テスト\n\n"
            "## 投資アイディア\n具体的なアイディア"
        )
        sections = DiscordNotifier._parse_idea_sections(text)
        assert "データソース" in sections
        assert "投資アイディア" in sections
        assert "具体的なアイディア" in sections["投資アイディア"]

    def test_truncate(self):
        """長いテキストが正しく切り詰められる"""
        long_text = "あ" * 2000
        result = DiscordNotifier._truncate(long_text, 100)
        assert len(result) == 100
        assert result.endswith("...")

    def test_truncate_short(self):
        """短いテキストはそのまま返される"""
        short_text = "短いテキスト"
        result = DiscordNotifier._truncate(short_text, 100)
        assert result == short_text

    def test_truncate_empty(self):
        """空テキストは 'ー' を返す"""
        result = DiscordNotifier._truncate("", 100)
        assert result == "ー"


# --- 遅延送信（バッチ送信）テスト ---

class TestDeferredNotifications:
    """遅延送信機能のテスト"""

    def setup_method(self):
        self.notifier = DiscordNotifier(
            summary_webhook_url="https://discord.com/api/webhooks/test/summary",
            idea_webhook_url="https://discord.com/api/webhooks/test/idea",
        )
        self.video_info = {
            "video_id": "abc123",
            "title": "テスト動画",
            "channel": "テストチャンネル",
            "published_at": "2026-03-15T11:00:00Z",
        }

    def test_queue_and_save_deferred(self, tmp_path):
        """通知をキューに溜めてJSONファイルに保存できる"""
        filepath = tmp_path / "deferred.json"

        # キューに追加
        self.notifier.queue_idea(self.video_info, "要約テスト", "# アイデア\n## 投資アイディア\nテスト")
        self.notifier.queue_skip(self.video_info)
        self.notifier.queue_summary([{"title": "t1", "idea": "✅ あり"}], 1, 1)

        # 保存
        self.notifier.save_deferred(filepath)

        # ファイルが作成されていることを確認
        assert filepath.exists()

        import json
        data = json.loads(filepath.read_text(encoding="utf-8"))
        assert len(data) == 3
        assert data[0]["type"] == "idea"
        assert data[1]["type"] == "skip"
        assert data[2]["type"] == "summary"

    @patch("discord_notifier.requests.post")
    def test_send_deferred(self, mock_post, tmp_path):
        """JSONファイルから読み込んで正しく送信される"""
        import json
        filepath = tmp_path / "deferred.json"

        # テスト用キューデータを直接ファイルに書き込む
        queue = [
            {
                "type": "idea",
                "video_info": self.video_info,
                "summary": "テスト要約",
                "idea_text": "# テスト\n## 投資アイディア\nテストアイデア",
            },
            {
                "type": "summary",
                "results": [{"title": "t", "idea": "✅", "channel": "c", "url": "u"}],
                "total_processed": 1,
                "total_ideas": 1,
            },
        ]
        filepath.write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")

        mock_post.return_value = MagicMock(status_code=204)

        sent = self.notifier.send_deferred(filepath)

        assert sent == 2
        assert mock_post.call_count == 2
        # ファイルが削除されていることを確認
        assert not filepath.exists()

    def test_send_deferred_no_file(self, tmp_path):
        """ファイルが存在しない場合は0を返してエラーにならない"""
        filepath = tmp_path / "nonexistent.json"
        sent = self.notifier.send_deferred(filepath)
        assert sent == 0

    def test_save_deferred_empty_queue(self, tmp_path):
        """キューが空の場合はファイルを作成しない"""
        filepath = tmp_path / "deferred.json"
        self.notifier.save_deferred(filepath)
        assert not filepath.exists()
