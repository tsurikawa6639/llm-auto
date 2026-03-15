"""IdeaExtractor のユニットテスト"""

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# テスト対象のモジュールが src/ にあるため、パスを通す
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from idea_extractor import IdeaExtractor


# --- save_idea の重複チェックテスト ---

class TestSaveIdeaDuplicate:
    """save_idea の重複防止ロジックをテストする"""

    def setup_method(self):
        """各テスト前にテンポラリディレクトリを用意"""
        self._tmpdir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self._tmpdir.name)
        self.extractor = IdeaExtractor(api_key="dummy", model="dummy")
        self.video_info = {
            "title": "テスト動画",
            "channel": "テストチャンネル",
            "published_at": "2026-03-15T11:00:00Z",
            "duration": "PT10M30S",
            "view_count": "1000",
        }

    def teardown_method(self):
        self._tmpdir.cleanup()

    @patch("idea_extractor.OUTPUT_DIR")
    def test_first_save_creates_file(self, mock_output_dir):
        """初回保存でファイルが作成される"""
        mock_output_dir.__truediv__ = lambda self_, name: self.output_dir / name
        mock_output_dir.mkdir = MagicMock()
        mock_output_dir.glob = lambda pattern: list(self.output_dir.glob(pattern))

        filepath = self.extractor.save_idea("abc123", self.video_info, "テストアイディア")
        assert filepath.exists()
        assert "abc123" in filepath.name

    @patch("idea_extractor.OUTPUT_DIR")
    def test_duplicate_save_returns_existing(self, mock_output_dir):
        """同じ video_id で2回目の保存は既存ファイルを返す（重複防止）"""
        mock_output_dir.__truediv__ = lambda self_, name: self.output_dir / name
        mock_output_dir.mkdir = MagicMock()
        mock_output_dir.glob = lambda pattern: list(self.output_dir.glob(pattern))

        # 1回目の保存
        first_path = self.extractor.save_idea("abc123", self.video_info, "アイディア1")
        # 2回目の保存（同じ video_id）
        second_path = self.extractor.save_idea("abc123", self.video_info, "アイディア2")

        # 同じファイルが返される（新規作成されない）
        assert first_path == second_path
        # ファイルは1つだけ
        md_files = list(self.output_dir.glob("*_abc123.md"))
        assert len(md_files) == 1

    @patch("idea_extractor.OUTPUT_DIR")
    def test_different_video_ids_create_separate_files(self, mock_output_dir):
        """異なる video_id ではそれぞれファイルが作成される"""
        mock_output_dir.__truediv__ = lambda self_, name: self.output_dir / name
        mock_output_dir.mkdir = MagicMock()
        mock_output_dir.glob = lambda pattern: list(self.output_dir.glob(pattern))

        path1 = self.extractor.save_idea("video_A", self.video_info, "アイディアA")
        path2 = self.extractor.save_idea("video_B", self.video_info, "アイディアB")

        assert path1 != path2
        assert path1.exists()
        assert path2.exists()


# --- _parse_response のテスト ---

class TestParseResponse:
    """_parse_response の分割ロジックをテストする"""

    def test_parse_with_ideas(self):
        """アイディアあり応答が正しくパースされる"""
        result = (
            "SUMMARY:\n"
            "動画では株式投資について解説している。\n\n"
            "IDEAS:\n"
            "# 半導体銘柄の投資チャンス\n"
            "## 投資アイディア\n"
            "半導体需要が拡大中\n"
        )
        summary, idea_text = IdeaExtractor._parse_response(result, {"title": "test"})
        assert "株式投資" in summary
        assert idea_text is not None
        assert "半導体" in idea_text

    def test_parse_with_no_ideas(self):
        """アイディアなし応答が正しくパースされる"""
        result = (
            "SUMMARY:\n"
            "投資の基本的な考え方についての一般論。\n\n"
            "IDEAS: NONE"
        )
        summary, idea_text = IdeaExtractor._parse_response(result, {"title": "test"})
        assert "一般論" in summary
        assert idea_text is None

    def test_parse_without_markers(self):
        """SUMMARY/IDEASマーカーがない場合のフォールバック"""
        result = "マーカーのないレスポンステキスト"
        summary, idea_text = IdeaExtractor._parse_response(result, {"title": "test"})
        # パースに失敗した場合、全文がアイディアとして扱われる
        assert idea_text == result
