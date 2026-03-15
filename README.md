# YouTube 定刻監視システム

YouTube上の投資関連動画を定期的に検索し、Gemini AIで内容を要約・投資アイディアを自動抽出するツール。

## 機能

- **定刻自動検索** — 指定キーワードで直近N時間の新着動画をYouTube Data API v3で検索
- **YouTube Shorts 除外** — 再生時間＋HEADリクエストの2段階フィルタでショート動画を確実に除外
- **字幕自動取得** — 日本語/英語の手動・自動生成字幕を取得（なければ概要欄にフォールバック）
- **AI要約＋投資アイディア抽出** — Gemini APIで動画内容を要約し、具体的な投資アイディアを判定・抽出
- **Gemini APIリクエスト制限** — 1回あたりの処理上限（デフォルト10件）を設定可能。超過分は保留キューに保存し、次回実行で優先処理
- **レポート出力** — 調査結果一覧＋各動画の要約をMarkdown/CSVで出力
- **スケジューラ実行** — APSchedulerで指定間隔での自動実行に対応

## ディレクトリ構成

```
youtube-monitor/
├── src/
│   ├── main.py                 # エントリーポイント（CLI / スケジューラ）
│   ├── youtube_monitor.py      # YouTube検索・Shorts判定・キュー管理
│   ├── transcript_fetcher.py   # 字幕取得
│   └── idea_extractor.py       # Gemini API 要約＋アイディア抽出
├── data/
│   ├── seen_videos.json        # 処理済み動画ID（自動生成）
│   └── pending_videos.json     # 保留キュー（自動生成）
├── output/
│   ├── report_YYYYMMDD_HHMMSS.md   # Markdownレポート
│   ├── report_YYYYMMDD_HHMMSS.csv  # CSVレポート
│   └── ideas/                       # 抽出されたアイディアファイル
├── config.yaml                 # 設定ファイル
├── .env                        # APIキー（Git管理外）
├── .env.example                # .envのテンプレート
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## セットアップ

### 1. APIキーの準備

| APIキー | 取得先 |
|---------|--------|
| `YOUTUBE_API_KEY` | [Google Cloud Console](https://console.cloud.google.com/) → YouTube Data API v3 を有効化 |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/) |

### 2. 環境変数の設定

```bash
cp .env.example .env
# .env を編集してAPIキーを設定
```

### 3. Docker起動

```bash
# ビルド
docker compose build

# スケジューラモードで起動（バックグラウンド）
docker compose up -d

# 1回だけ実行
docker compose run --rm youtube-monitor python src/main.py --once
```

## 使い方

### コマンドラインオプション

```bash
# デフォルトキーワード（config.yaml）でスケジューラ実行
python src/main.py

# 1回だけ実行
python src/main.py --once

# キーワードを指定して1回実行
python src/main.py --once -k "半導体 決算" -k "AI関連 株"
```

| オプション | 説明 |
|-----------|------|
| `--once` | 1回だけ実行して終了（スケジューラを使わない） |
| `-k`, `--keyword` | 検索キーワード（複数指定可）。未指定時は `config.yaml` のデフォルト値を使用 |

## 設定（config.yaml）

```yaml
youtube:
  search_keywords:          # デフォルトの検索キーワード
    - "株式投資 分析"
    - "決算 銘柄"
    - "テクニカル分析 日本株"
  max_results_per_search: 10  # キーワードあたりの最大検索結果数
  published_after_hours: 2    # 直近何時間以内の動画を検索するか

schedule:
  interval_hours: 1           # スケジューラの実行間隔（時間）

gemini:
  model: "gemini-3-flash-preview"
  temperature: 0.3
  max_requests_per_run: 10    # 1回の実行あたりのGemini APIリクエスト上限
```

## 処理フロー

```
1. 保留キュー読み込み（前回未処理の動画を優先取得）
2. 全キーワードでYouTube検索 → 新着動画を収集
3. Shorts除外（再生時間180秒以下 → HEADリクエストで確認）
4. 保留キュー（優先）＋ 新着動画を結合
5. 先頭10件（上限）の動画に対して：
   a. 字幕/概要欄を取得
   b. Gemini APIで要約＋投資アイディア判定
   c. アイディアがあればファイル保存
6. 11件目以降を保留キューに保存（次回実行で優先処理）
7. Markdown/CSVレポートを出力
```

## 出力

### Markdownレポート（`output/report_*.md`）

- **調査結果一覧** — 全動画のタイトル・チャンネル・アイディア有無を一覧表示
- **動画詳細** — 各動画のGemini AIによる要約全文を表示

### アイディアファイル（`output/ideas/*.md`）

投資アイディアが抽出された動画ごとに個別のMarkdownファイルを生成。データソース・根拠・因果関係を含む。

## 依存ライブラリ

| ライブラリ | 用途 |
|-----------|------|
| `google-api-python-client` | YouTube Data API v3 |
| `youtube-transcript-api` | YouTube字幕取得 |
| `google-generativeai` | Gemini API |
| `apscheduler` | 定刻スケジューラ |
| `requests` | YouTube Shorts判定（HEADリクエスト） |
| `python-dotenv` | 環境変数管理 |
| `pyyaml` | 設定ファイル読み込み |
