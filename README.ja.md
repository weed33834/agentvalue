[English](README.md) | [中文](README.zh.md) | [日本語](README.ja.md)

<p align="center">
  <img src="docs/assets/logo.jpg" width="180" alt="AgentValue-AI Logo" />
</p>

<h1 align="center">AgentValue-AI</h1>

<p align="center">
  対話、コンピュータ操作、従業員価値評価を一つに統合した AI エージェントプラットフォーム<br/>
  ChatGPT / Claude.ai / opencode / Dify / Coze を参照系とする
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-CNCL%20v1.0-red.svg" alt="License" /></a>
  <a href="https://github.com/weed33834/agentvalue/actions/workflows/ci.yml"><img src="https://github.com/weed33834/agentvalue/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://github.com/weed33834/agentvalue/actions/workflows/security.yml"><img src="https://github.com/weed33834/agentvalue/actions/workflows/security.yml/badge.svg" alt="Security" /></a>
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/Node-20+-339933?logo=node.js&logoColor=white" alt="Node" />
  <img src="https://img.shields.io/badge/FastAPI-0.139+-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Vue-3-4FC08D?logo=vue.js&logoColor=white" alt="Vue 3" />
  <img src="https://img.shields.io/badge/LangGraph-agent-FF6B6B" alt="LangGraph" />
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker" />
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/version-2.2.0-blue.svg" alt="Version" /></a>
  <a href="CONTRIBUTING.md"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome" /></a>
</p>

---

## 概要

AgentValue-AI は三つの能力領域を単一のプラットフォームに統合しています。

**対話** — ストリーミング出力、ツール呼び出しの可視化、折りたたみ可能な推論プロセス、数式レンダリング、Markdown エクスポートを備えたフル機能の AI チャットインターフェース。ChatGPT や Claude.ai で日常的に使われる操作を網羅しています。

**ツール利用** — エージェントは bash コマンドの実行、ファイルの読み書き、ディレクトリ一覧、ウェブページの取得、コード検索、Python サンドボックスの実行を行います。組み込みツールが日常操作をカバーし、MCP プロトコルにより 400 以上の外部ツールを統合できます。本番環境では高危険ツールを環境ごとに切り替え可能です。

**従業員価値評価** — プラットフォームは従業員の多次元な業務データ（日報、タスク進捗、コード貢献、議事録、スクリーンショット、音声）を継続的に受け取り、LangGraph エージェントで分析し、一回の推論で三つのビューを同時に生成します。

| ビュー | 対象 | 内容 |
|---|---|---|
| 従業員ビュー | 本人 | 建設的な成長フィードバック |
| 管理職ビュー | 上司 / HR | 人材診断と配置転換の提案 |
| 監査ビュー | コンプライアンス / 監査 | 各結論に原文証拠の引用付き、追跡可能 |

三つのビューは意図的に分離されています。同じ所見も、従業員には「成長余地」、管理職には「ROI 低下」と表現が変わります。言葉遣いと立場は対象ごとに異なります。すべての評価は人による承認を経て初めて有効になります。これは推奨ではなく硬い制約です。

---

## 機能一覧

### AI チャット

| 機能 | 説明 |
|---|---|
| ストリーミング対話 | SSE によるトークン単位の出力、中断対応 |
| ツール呼び出し表示 | 折りたたみ可能な入出力、JSON 整形、ステータスアイコン |
| 推論プロセス | DeepSeek の reasoning_content を折りたたみ表示 |
| メッセージコピー | コードブロックコピーとメッセージ全体コピー |
| 再生成 | 末尾の返信を削除して再実行 |
| メッセージ編集 | ユーザーメッセージのインライン編集 |
| トークン使用量 | メッセージごとにトークン内訳と応答遅延を表示 |
| セッション管理 | リネーム、自動タイトル、検索、Markdown エクスポート |
| 数式 | KaTeX インライン `$...$` とブロック `$$...$$` |
| チャートレンダリング | Mermaid フローチャート、シーケンス図の遅延読み込み |
| フィードバック | like / dislike、永続化 |
| ファイルアップロード | 複数ファイル添付、10 MB 上限 |
| モデル切替 | ドロップダウンで 8 モデルを切替 |

### エージェントツール

| ツール | 説明 | 安全制約 |
|---|---|---|
| `bash` | シェルコマンドの実行 | 30 秒タイムアウト + 5000 文字切り詰め |
| `read_file` | ファイル内容の読み取り | 5000 文字切り詰め |
| `write_file` | ファイルへの書き込み | 親ディレクトリを自動生成 |
| `list_directory` | ディレクトリ内容の一覧 | — |
| `web_fetch` | ウェブページの取得 | HTML → プレーンテキスト + 切り詰め |
| `calculator` | 数式計算 | — |
| `get_current_datetime` | 現在日時の取得 | — |
| `get_employee_history` | 従業員の評価履歴の照会 | 業務ツール |
| `query_company_kb` | 社内ナレッジベースの照会 | 業務ツール |

ツールは `ToolRegistry` で一元管理され、`enabled_tools` 設定で切り替え可能です。

### 運用管理プラットフォーム

| ページ | ルート | 参照系 |
|---|---|---|
| モデルプロバイダー | `/admin/providers` | Dify model-providers |
| Prompt プレイグラウンド | `/admin/playground` | Langfuse Playground |
| ナレッジベース管理 | `/admin/knowledge-base` | Dify Dataset |
| トレース | `/admin/trace` | Langfuse Trace |
| トークン推移 | `/admin/metrics` | Langfuse Usage |
| フィーチャーフラグ | `/admin/feature-flags` | LaunchDarkly |
| マルチエージェント連携 | `/admin/multi-agent` | LangGraph Supervisor |
| ワークフロー編成 | `/admin/workflows` | Dify Workflow |
| カスタムツール | `/admin/tools` | Dify Custom Tool |
| モデルフォールバック | `/admin/model-fallback` | Alibaba Bailian AI Gateway |
| セッション分析 | `/admin/analytics-v2` | Langfuse Dashboard |
| API ヘルス | `/admin/api-health` | Langfuse レイテンシ監視 |
| データセット管理 | `/admin/datasets` | Langfuse データセット |
| LLM 評価 | `/admin/llm-judge` | Langfuse LLM-as-a-Judge |
| RAG 評価 | `/admin/rag-eval` | RagFlow 検索テスト |
| 人手アノテーション | `/admin/annotations` | Langfuse HITL |
| SSO 設定 | `/admin/sso` | Dify SSO |
| テンプレートマーケット | `/admin/agent-templates` | Coze プラグイン市場 |
| NL2SQL | `/admin/nl2sql` | RagFlow NL2SQL |
| ドキュメント解析 | `/admin/doc-parsing` | RagFlow DeepDoc |
| クォータ管理 | `/admin/quota` | Alibaba Bailian Token Plan |
| 予算アラート | `/admin/budgets` | Langfuse 予算 |
| 課金請求 | `/admin/billing` | Alibaba Bailian 統一計量 |
| エージェントバージョン | `/admin/agent-versions` | Langfuse バージョン |
| 多チャネル配信 | `/admin/publish` | Coze 全域配信 |
| 機密語管理 | `/admin/sensitive-words` | Tencent Hunyuan コンテンツ安全 |
| アラート通知 | `/admin/alerts` | Grafana Alerting |
| ツール設定 | `/admin/tool-config` | Dify ツール管理 |
| ハイブリッド検索 | `/admin/search` | Dify 検索 |

---

## システムアーキテクチャ

```
┌──────────────────────────────────────────────────────────┐
│        フロントエンド層 (Vue 3 + Element Plus)             │
│  従業員 │ 管理職 │ HR │ 管理コンソール │ AI チャット UI     │
├──────────────────────────────────────────────────────────┤
│        API ゲートウェイ層 (FastAPI)                       │
│  RBAC │ レート制限 │ 監査ログ │ ガード │ SSE ストリーム     │
├──────────────────────────────────────────────────────────┤
│     エージェント編成層 (LangGraph + ReAct ループ)         │
│  状態機械 │ ツール呼び出し │ 記憶検索 │ HITL               │
├──────────────────────────────────────────────────────────┤
│        エージェントツール層 (組み込み 9 ツール)            │
│  bash │ read_file │ write_file │ list_directory │ web_fetch│
│  calculator │ datetime │ employee_history │ company_kb    │
├──────────────────────────────────────────────────────────┤
│        モデル抽象層 (ModelRouter)                         │
│  ハードウェア検出 │ クラウド API │ ローカル LM Studio │ フォールバック │
├──────────────────────────────────────────────────────────┤
│        データと記憶層                                     │
│  SQLite/PostgreSQL │ ChromaDB │ Redis (キュー)            │
└──────────────────────────────────────────────────────────┘
```

---

## 技術スタック

| 層 | 技術 |
|---|---|
| フロントエンド | Vue 3 + JavaScript + Vite + Element Plus + ECharts + Vue Flow + KaTeX + Mermaid |
| バックエンド | Python 3.11+ + FastAPI + SQLAlchemy |
| エージェント | LangGraph (supervisor マルチエージェント + ReAct ループ + SSE ストリーミング) |
| LLM プロバイダー | OpenAI / Anthropic Claude / Google Gemini / Ollama (暗号化クレデンシャル + 負荷分散) |
| Rerank | Cohere / Jina / BGE (ローカル) / Dummy フォールバック |
| ストリーミング | sse-starlette + @microsoft/fetch-event-source |
| ベクトル記憶 | ChromaDB |
| データベース | SQLite (既定) / PostgreSQL (本番) |
| キャッシュ | Redis (ジョブキュー、未設定時はメモリにフォールバック) |
| 可観測性 | Prometheus + Langfuse + Grafana |
| ワークフローエンジン | 自前 DAG 実行器 (Kahn トポロジカルソート + 7 ノード種 + コードサンドボックス) |
| フィーチャーフラグ | 自前 5 段階ルール (sha256 安定ハッシュ + 60 秒 LRU キャッシュ) |
| テスト | pytest + locust (1517 + 122 enterprise = 1639 passing) |
| デプロイ | Docker Compose |
| 安全ガードレール | InputGuard + OutputGuard (PII マスキング / 脱獄防御 / バイアス検出 / ハルシネーションフラグ) |

---

## クイックスタート

### Docker Compose で一発起動

```bash
git clone https://github.com/weed33834/agentvalue.git
cd agentvalue
cp backend/.env.example backend/.env
docker compose up -d --build
```

起動後、以下のサービスが利用可能になります。

| サービス | URL |
|---|---|
| フロントエンド | <http://localhost> |
| バックエンド API | <http://localhost:8000> |
| ヘルスチェック | <http://localhost:8000/health> |
| Swagger UI | <http://localhost:8000/docs> |

### ローカル開発

**バックエンド:**

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                  # モデル API キーを入力
uvicorn main:app --reload --port 8000
```

**フロントエンド:**

```bash
cd frontend
npm install
npm run dev                           # http://localhost:5173
```

### API キーなしでの実行

モデル API キーを一切設定しない場合、システムは自動的に Mock Provider を使用し、評価フローをエンドツーエンドで実行します。

```bash
cd backend
cp .env.example .env
AUTH_DEMO_MODE=true uvicorn main:app --reload
python -m eval.evaluate --mock        # 外部 API を使わずに評価を実行
```

> デモモードはローカル開発専用です。`AUTH_DEMO_MODE=true` は HTTP ヘッダーによる身元偽装を許可するため、ローカル以外の環境にデプロイする前に必ず無効化してください。

---

## 設定

すべての設定は `backend/.env` から注入されます。項目ごとの注釈は [backend/.env.example](backend/.env.example) を参照してください。

### 主要設定

| 変数 | 用途 | 変更が必須な場面 |
|---|---|---|
| `JWT_SECRET_KEY` | JWT 署名鍵 | 本番デプロイ |
| `AGENTVALUE_ENV` | `production` に設定すると本番ガードを有効化 | 本番デプロイ |
| `CLOUD_API_KEY` | クラウド LLM (OpenAI 互換) | 実モデルが必要な場合 |
| `EMBEDDING_API_KEY` | 実 Embedding サービス | 意味検索が必要な場合 |
| `CORS_ORIGINS` | フロントエンド許可オリジン | 本番では実ドメインが必須 |
| `FIELD_ENCRYPTION_KEY` | 機密フィールドの AES-GCM 暗号化 | 本番では必須 |

### モデルティア

`MODEL_TIER` は評価 LLM をクラウドかローカルのどちらで動かすかを制御します。

| ティア | シナリオ | モデル例 |
|---|---|---|
| `auto` | ハードウェアから自動推奨 (既定) | — |
| `L0` | クラウド大モデル | GPT-4o / DeepSeek-V3 / Qwen-Max |
| `L1` | エッジ小モデル | Qwen2.5-0.5B |
| `L2` | 標準ローカルモデル | Qwen2.5-7B |
| `L3` | ローカル旗艦モデル | Qwen2.5-14B |

`CLOUD_API_KEY` と `LOCAL_BASE_URL` がどちらも未設定の場合は Mock Provider が使われ、外部モデルは不要です。

---

## 利用ガイド

### 1. 初期データのシード

```bash
python -m scripts.seed_kb             # 社内ナレッジベース (採点基準、バリュー、研修資料)
python -m scripts.seed_demo            # デモデータ (ユーザーと 1 件のサンプル評価)
```

### 2. ログイン

四つのロール: `employee` / `manager` / `hr` / `admin`。

デモモードではログインページに「デモアカウント自動入力」ボタンがあります。通常モードでは `/api/v1/auth/register` と `/api/v1/auth/login` で JWT を取得します。

### 3. 評価の開始

```bash
curl -X POST http://localhost:8000/api/v1/evaluations \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "employee_id": "E1001",
    "period": "2026-W25",
    "raw_inputs": [
      {"type": "daily_report", "content": "本日オーダーセンター API のリファクタリングを実施..."},
      {"type": "task_progress", "content": "JIRA-2051 を結合テスト段階に移行..."}
    ]
  }'
```

評価は LangGraph の状態機械に入ります。

```
input_clean → multimodal_extract → llm_evaluate → parse_output → persist
                       ↑                                ↓
                   retrieve_context            human-in-the-loop interrupt
```

### 4. 承認と三つのビュー

評価ステータスの遷移:

```
ai_drafted → manager_review → hr_audit (高リスク時のみ) → approved/rejected
                                  ↓ rejected
                            employee_appeal → manager_review
```

三つのビューを確認:

```bash
curl http://localhost:8000/api/v1/evaluations/{id} \
  -H "Authorization: Bearer <token>"
# レスポンスの employee_view / manager_view / audit_view が三つのビューです。
# フィールドレベルの可視性は RBAC で制御され、従業員トークンでは manager_view / audit_view は見えません。
```

### 5. AI チャット

管理コンソールの `/admin/chat` で完全なチャットインターフェースを提供します。セッション作成後にメッセージを送信:

```bash
# セッション作成
curl -X POST http://localhost:8000/api/v1/chat/sessions \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"title": "テストセッション", "model_name": "DeepSeek-V4-Flash"}'

# メッセージ送信 (SSE ストリーミング応答)
curl -X POST http://localhost:8000/api/v1/chat/sessions/{id}/messages \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"content": "現在のディレクトリのファイル一覧を表示して"}'
```

エージェントは自動的に `list_directory` ツールを呼び出し、整形結果を返します。

### 6. 可観測性

- Prometheus メトリクス: <http://localhost:8000/metrics> (21 項目の業務メトリクス)
- Grafana ダッシュボード: 本番 compose 起動後に <http://localhost:3000>
- Langfuse トレース: `LANGFUSE_*` を設定すると自動的に報告
- 監査ログ: すべての書き込み操作が監査テーブルに記録され、管理コンソールからページング検索可能

---

## テスト

```bash
cd backend && python -m pytest tests -q          # ユニットテスト
cd backend && python -m pytest -m e2e -q         # E2E テスト
cd backend && python -m eval.evaluate --mock      # モック評価
cd frontend && npm run lint                       # フロントエンド lint
cd frontend && npm run build                      # フロントエンドビルド
```

バックエンドは 1517 件のテストがパス、フロントエンドのビルドと lint はエラーなしです。

---

## 本番デプロイ

```bash
cp backend/.env.example backend/.env
# .env を編集し、本番用クレデンシャルをすべて設定
cd backend && python scripts/check_prod_readiness.py   # レディネスチェック
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

本番スタックはベースの compose の上に PostgreSQL、MinIO、Prometheus + Grafana を重ねます。

詳細なデプロイガイド: [エンタープライズデプロイ](docs/deployment-guide.md) | [パイロット Runbook](docs/pilot-runbook.md) | [スケールアウト Runbook](docs/scale-deployment-runbook.md)

---

## プロジェクト構成

```
.
├── backend/
│   ├── agent/            # LangGraph エージェント + ReAct ループ + ツール呼び出し
│   ├── api/              # FastAPI ルート (chat / auth / admin/*)
│   ├── auth/             # JWT + RBAC
│   ├── core/             # 設定 / モデルルータ / ガード / ワークフローエンジン / フィーチャーフラグ
│   ├── models/           # SQLAlchemy データモデル
│   ├── services/         # 業務サービス
│   ├── tests/            # 1517 + 122 enterprise = 1639 passing
│   └── ...
├── frontend/
│   ├── src/components/chat/   # チャットコンポーネント
│   ├── src/stores/chat.js     # チャット状態管理
│   ├── src/utils/markdown.js  # KaTeX + Mermaid レンダリング
│   └── src/views/admin/       # 管理コンソールページ
├── docs/                 # プロジェクトドキュメント
├── monitoring/           # Prometheus 設定
├── grafana/              # Grafana ダッシュボード
├── .github/              # CI / Issue テンプレート / PR テンプレート
├── docker-compose.yml    # 開発スタック
├── docker-compose.prod.yml
└── CHANGELOG.md
```

---

## ドキュメント一覧

| ドキュメント | 説明 |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | バージョン変更履歴 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | コントリビューションガイド |
| [SECURITY.md](SECURITY.md) | セキュリティ脆弱性報告フロー |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 行動規範 |
| [backend/README.md](backend/README.md) | バックエンド開発メモ |
| [frontend/README.md](frontend/README.md) | フロントエンド開発メモ |
| [docs/architecture-notes.md](docs/architecture-notes.md) | アーキテクチャ実装メモ |
| [docs/deployment-guide.md](docs/deployment-guide.md) | エンタープライズデプロイガイド |
| [docs/dev-guidelines.md](docs/dev-guidelines.md) | 開発ガイドライン |
| [docs/DEVELOPMENT-PLAN.md](docs/DEVELOPMENT-PLAN.md) | 開発計画 |

---

## ロードマップ

**リリース済みバージョン:**

- v1.2 — モデルプロバイダー管理 + Prompt プレイグラウンド + マルチプロバイダー統合
- v1.3 — arq ジョブキュー + Postgres 永続化 + テスト補完 + CI 強化 + Feishu/GitLab 連携スケルトン
- v1.4 — ナレッジベース UI / トレース / トークン推移 / Rerank / カスタムツール / フィーチャーフラグ / マルチエージェント / ワークフロー編成
- v1.5 — AI チャットシステム (10 機能) + エージェントツール層 (5 ツール)
- v2.1 — 主要プラットフォームとの深いパリティで管理機能マトリクスを拡充: モデルフォールバック / セッション分析 / API ヘルス / データセット / LLM 評価 / RAG 評価 / 人手アノテーション / SSO / テンプレートマーケット / NL2SQL / ドキュメント解析 + 19 項目のセキュリティ強化

**今後の方向性:**

- チャット添付ファイルの解析 (画像 / PDF / 音声)
- ストリーミング中断の復旧と会話の分岐
- 会話シェアリンク
- マルチモーダル能力の補完 (クラウド OCR / Whisper ASR)
- チーム ROI 九分割と成長パスダッシュボードの拡充
- IM 連携の本格化 (Feishu)
- コードリポジトリ連携の本格化 (GitLab)

推進したい方向があれば、[GitHub Issues](https://github.com/weed33834/agentvalue/issues) から提案してください。

---

## FAQ

**API キーなしで動きますか?**

動きます。システムは既定で Mock Provider を使用し、評価フローをエンドツーエンドで実行しますが、LLM 出力はシミュレートされます。実運用には `CLOUD_API_KEY` または `LOCAL_BASE_URL` の設定が必要です。

**評価結果を人事判断に直接使えますか?**

使えません。「AI は人事判断を行わない」が中核の硬い制約です。すべての評価は管理職の承認を経る必要があり、高リスク項目はさらに HR レビューが必要です。エージェントは生成と構造化のみを行い、人に代わって結論を下すことはありません。

**エージェントの bash ツールは安全ですか?**

30 秒のタイムアウトと 5000 文字の出力切り詰めを設定しています。すべてのツールは `ToolRegistry` で一元管理され、`enabled_tools` で切り替え可能です。本番環境では `calculator,get_current_datetime` などの安全なツールのみを有効化できます。

**AI チャットはどのモデルに対応していますか?**

既定は `DeepSeek-V4-Flash` (OpenAI 互換ゲートウェイ)。フロントエンドのドロップダウンは DeepSeek V4 Flash/Pro、GLM 4.7/5.1、Qwen 3 Coder、Kimi K2.6、MiniMax M3 に対応します。OpenAI 互換 API と function calling をサポートするプロバイダーであれば統合できます。

**マルチテナント分離はどう実装されていますか?**

データ層ではすべてのテーブルに `tenant_id` カラムがあり、RBAC がデータレベルでフィルタします。ベクトルストアはテナントごとに collection を分け、ジョブキューはキーにテナントのプレフィックスを付けます。

---

## コントリビューション

Issue とプルリクエストを歓迎します。開始前に [CONTRIBUTING.md](CONTRIBUTING.md) をお読みください。CI は各 PR で lint / テスト / ビルドを自動実行し、すべて緑にならないとマージできません。

---

## セキュリティ

セキュリティ脆弱性を発見した場合は、[SECURITY.md](SECURITY.md) の手順に従い非公開で報告してください。公開 Issue は開かないでください。

---

## ミラー

| プラットフォーム | URL | 備考 |
|---|---|---|
| GitCode (主リポジトリ) | <https://gitcode.com/badhope/agentvalue> | Issue / PR 提出 |
| GitHub (ミラー) | <https://github.com/weed33834/agentvalue> | 国際ミラー |

---

## ライセンス

本プロジェクトは [Custom Non-Commercial License (CNCL) v1.0](LICENSE) の下で公開されています。© 2026 AgentValue-AI Contributors.
