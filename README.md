# YouTube 定刻監視システム

YouTube上の投資関連動画を定期的に検索し、Gemini AIで内容を要約・投資アイディアを自動抽出するツール。
GitHub Actionsによるクラウド定刻実行とDiscord通知に対応。

## 機能

- **定刻自動検索** — 指定キーワードで直近N時間の新着動画をYouTube Data API v3で検索
- **YouTube Shorts 除外** — `videoDuration` APIパラメータ（medium / long）で検索段階からショート動画を除外
- **AI要約＋投資アイディア抽出** — Gemini APIに動画URLを直接渡し、動画内容を要約＋具体的な投資アイディアを判定・抽出（動画URLで処理できない場合は概要欄テキストにフォールバック）
- **Gemini APIリクエスト制限** — 1回あたりの処理上限（デフォルト10件）を設定可能。超過分は保留キューに保存し、次回実行で優先処理
- **Discord通知** — サマリーチャンネルに実行結果レポート、アイデアチャンネルに投資アイディアを個別送信
- **レポート出力** — 調査結果一覧＋各動画の要約をMarkdown/CSVで出力
- **GitHub Actions定刻実行** — 2時間ごとの自動実行＋手動トリガーに対応。古いレポートの自動クリーンアップ付き
- **ローカル実行** — Dockerまたはスケジューラ（APScheduler）でのローカル実行にも対応

## ディレクトリ構成

```
youtube-monitor/
├── src/
│   ├── main.py                 # エントリーポイント（CLI / スケジューラ）
│   ├── youtube_monitor.py      # YouTube検索・重複排除・キュー管理
│   ├── idea_extractor.py       # Gemini API 要約＋アイディア抽出
│   └── discord_notifier.py     # Discord Webhook 通知
├── test/
│   ├── test_discord_notifier.py
│   └── test_idea_extractor.py
├── data/
│   ├── seen_videos.json        # 処理済み動画ID（自動生成）
│   └── pending_videos.json     # 保留キュー（自動生成）
├── output/
│   ├── report_YYYYMMDD_HHMMSS.md   # Markdownレポート
│   ├── report_YYYYMMDD_HHMMSS.csv  # CSVレポート
│   └── ideas/                       # 抽出されたアイディアファイル
├── .github/
│   └── workflows/
│       └── youtube-monitor.yml # GitHub Actions ワークフロー
├── config.yaml                 # 設定ファイル
├── .env                        # APIキー・Webhook URL（Git管理外）
├── .env.example                # .envのテンプレート
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## セットアップ

### 1. APIキー・Webhook URLの準備

| キー | 取得先 |
|------|--------|
| `YOUTUBE_API_KEY` | [Google Cloud Console](https://console.cloud.google.com/) → YouTube Data API v3 を有効化 |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/) |
| `DISCORD_WEBHOOK_URL` | Discordサーバー設定 → 連携サービス → ウェブフック（サマリー用） |
| `DISCORD_IDEA_WEBHOOK_URL` | 同上（投資アイデア用チャンネル） |

### 2. 環境変数の設定

```bash
cp .env.example .env
# .env を編集してAPIキーとWebhook URLを設定
```

### 3A. Docker起動（ローカル実行）

```bash
# ビルド
docker compose build

# スケジューラモードで起動（バックグラウンド）
docker compose up -d

# 1回だけ実行
docker compose run --rm youtube-monitor python src/main.py --once
```

### 3B. GitHub Actions（クラウド実行）

1. GitHubリポジトリの **Settings → Secrets and variables → Actions** で以下のSecretsを設定:
   - `YOUTUBE_API_KEY`
   - `GEMINI_API_KEY`
   - `DISCORD_WEBHOOK_URL`
   - `DISCORD_IDEA_WEBHOOK_URL`
2. ワークフローは2時間ごとに自動実行（`workflow_dispatch` で手動実行も可能）
3. 実行結果（`data/` `output/`）は自動的にコミット＆プッシュされる

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
  search_keywords:              # デフォルトの検索キーワード
    - "株"
  max_results_per_search: 50    # キーワードあたりの最大検索結果数
  published_after_hours: 3      # 直近何時間以内の動画を検索するか

schedule:
  interval_hours: 1             # スケジューラの実行間隔（時間）

gemini:
  model: "gemini-3.1-flash-lite-preview"
  temperature: 0.3
  max_requests_per_run: 10      # 1回の実行あたりのGemini APIリクエスト上限

discord:
  enabled: true                 # false にすると Discord 通知を一時停止
```

## 処理フロー

```
1. 保留キュー読み込み（前回未処理の動画を優先取得）
2. 全キーワードでYouTube検索 → 新着動画を収集（キーワード間の重複排除あり）
3. videoDuration パラメータ（medium/long）でShorts除外済み
4. 保留キュー（優先）＋ 新着動画を結合
5. 先頭10件（上限）の動画に対して：
   a. 動画URLを直接Gemini APIに渡して要約＋投資アイディア判定
      （動画URLが処理できない場合は概要欄テキストにフォールバック）
   b. アイディアがあればファイル保存
   c. Discord アイデアチャンネルに送信
6. 11件目以降を保留キューに保存（次回実行で優先処理）
7. Markdown/CSVレポートを出力
8. Discord サマリーチャンネルに実行結果を送信
```

## 出力

### Markdownレポート（`output/report_*.md`）

- **調査結果一覧** — 全動画のタイトル・チャンネル・アイディア有無を一覧表示
- **動画詳細** — 各動画のGemini AIによる要約全文を表示

### アイディアファイル（`output/ideas/*.md`）

投資アイディアが抽出された動画ごとに個別のMarkdownファイルを生成。データソース・根拠・因果関係を含む。

### Discord通知

- **サマリーチャンネル** — 実行完了ごとに処理件数・アイデア数・各動画の結果をEmbed形式で1投稿
- **アイデアチャンネル** — 投資アイデア1件につき1投稿（動画情報・要約・アイデア詳細を含む）

## 依存ライブラリ

| ライブラリ | 用途 |
|-----------|------|
| `google-api-python-client` | YouTube Data API v3 |
| `google-genai` | Gemini API（動画URL直接解析） |
| `apscheduler` | 定刻スケジューラ |
| `requests` | Discord Webhook送信 |
| `python-dotenv` | 環境変数管理 |
| `pyyaml` | 設定ファイル読み込み |
