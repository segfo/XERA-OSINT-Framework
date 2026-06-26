# OSINT ツール 使い方ガイド

2つのオーケストレーター（`osint-agent.py` / `osint-survey.py`）の使い分け・実行方法・
途中再開・ログイン/CAPTCHA 対応をまとめたガイドです。

> サイト調査の詳細手順は [SURVEY-MANUAL.md](SURVEY-MANUAL.md) を参照。

---

## 1. どっちを使う？ — 2ツールの違い

| | **osint-agent.py** | **osint-survey.py** |
|---|---|---|
| 起点 | **自然言語クエリ**（テーマ） | **1つのサイトURL** |
| 調査範囲 | 複数ソース横断（deepdarkCTI + 表層検索 + Tor検索 + ログイン済みフォーラム） | 指定サイト1つの内部を網羅 |
| 出力 | クエリへの回答レポート | サイトの動向レポート |
| 指定方法 | `--query "Clopについて"` | `--site breached-su --url https://breached.su` |
| 例えるなら | 「テーマで調べる調査員」 | 「1サイトを棚卸しする調査員」 |

### 使い分けの目安

```
「Clopランサムウェアについて知りたい」      → osint-agent.py   （テーマ駆動・横断）
「日本企業へのランサムウェア攻撃の動向」     → osint-agent.py
「breached.su の中を一通り調べたい」        → osint-survey.py  （サイト棚卸し）
「特定フォーラムの最新リーク傾向を把握」     → osint-survey.py
```

---

## 2. 共通の前提

### 共有バックエンド（Tor + ブラウザ常駐）
両ツールとも、Tor とブラウザは**共有バックエンド**（`localhost:9100`）に常駐します。
最初にツールを使ったプロセスが自動起動し、以降の `claude -p` も対話コンソールも
**同じブラウザ・ログイン状態を共有**します。事前起動は不要です。

- 明示的に止める場合: `python -c "from tor_mcp.config import TorConfig; from tor_mcp import backend_client; backend_client.shutdown_backend(TorConfig.from_env())"`
- ログ: `tor_mcp/vendor/backend.log`

### ブラウザの選択（Tor / Direct とエンジン）

ページ取得・閲覧は、検索（Tavily/Brave）で見つけた URL を見に行くときに使う。接続は2系統:

| 接続 | いつ | ブラウザ |
|------|------|---------|
| **Tor**    | `.onion`、または Direct が失敗したとき | Camoufox 固定（アンチ指紋） |
| **Direct** | 通常のクリアネット URL | エンジンを設定で選択（既定 **cdp**） |

`browser_open(via=...)` は `auto`（既定・自動判定）/ `tor` / `direct`。通常は `auto` でよい。

#### Direct のエンジン（`TOR_MCP_DIRECT_BROWSER`、`.mcp.json` で設定）

| 値 | 中身 | 用途 |
|----|------|------|
| **`cdp`（既定・推奨）** | 実 Chrome を `--remote-debugging-port` で自動起動し CDP 接続 | 拡張・ログインが生き **Bot検知されにくい**（pixiv 等） |
| `system` | Playwright が Chrome を起動 | 手軽だが `navigator.webdriver=true`/`--no-sandbox` で検知されやすい |
| `camoufox` | アンチ指紋 Firefox | 拡張・実プロファイル不要で匿名性重視 |

#### cdp の使い方（実 Chrome で調査する）

1. **`.mcp.json` 反映**：`TOR_MCP_DIRECT_BROWSER=cdp` 設定後は、tor-osint MCP サーバーを
   **再接続/再起動**する（env はサーバー起動時に読まれるため）。
2. **初回セットアップ（1回だけ）**：最初の `browser_open` で専用プロファイルの Chrome が
   自動起動する（既定 User Data = `tor_mcp/vendor/cdp-chrome-profile`、日常 Chrome と共存可）。
   その Chrome ウィンドウで **調査対象サイトにログイン・必要な拡張機能を導入**しておく。
   以後その状態は永続する。
3. **確認**：`browser_list_profiles` の `direct` エントリで `engine="cdp"`、`cdp_endpoint_alive`、
   `user_data_dir`、`profile_directory` を確認できる。

#### 特定プロファイル／実プロファイルを使う（`.mcp.json` の env）

```jsonc
"TOR_MCP_DIRECT_BROWSER": "cdp",
"TOR_MCP_CDP_PORT": "9222",
// 専用 User Data 内で自分が作ったプロファイルを使う場合
"TOR_MCP_CHROME_PROFILE_DIR": "Profile 1",
// 日常の実プロファイル（ログイン済み）を使う場合 ↓（使用中は日常 Chrome を全部閉じる）
"TOR_MCP_CHROME_USER_DATA_DIR": "C:\\Users\\<user>\\AppData\\Local\\Google\\Chrome\\User Data",
"TOR_MCP_CHROME_PROFILE_DIR": "Default"
```

> **⚠️ 排他ロック**: Chrome は User Data ディレクトリ単位でロックする。**同じ User Data の
> Chrome が起動中**だと `--remote-debugging-port` が無視されて CDP が開かない。既定の専用
> User Data なら日常 Chrome と共存できる。実 User Data を使うときは日常 Chrome を全て閉じる。

詳細は [tor_mcp/docs/BROWSERS.md](tor_mcp/docs/BROWSERS.md)。

### ローカルLLM（LM Studio）で動かす
`--lm-studio` を付けると `Invoke-ClaudeLocal` 相当（`ANTHROPIC_BASE_URL=http://localhost:1234`）で実行します。

```bash
# 既定モデル
uv run python osint-agent.py --query "..." --lm-studio

# モデル指定
uv run python osint-agent.py --query "..." --lm-studio --lm-model <モデル名>
```

---

## 3. osint-agent.py（クエリ駆動・横断調査）

### フェーズ（4エージェント・パイプライン）
各フェーズは独立した claude セッション。コンテキスト肥大を防ぐため収集・解析・まとめを分離する。
```
Phase 0  計画   クエリ → 調査計画           → plan.json（収集URL一覧）
Phase 1  収集   各URLをブラウザ操作で生データ保存 → raw/NNN-MM-*.md（本文はファイルへ、LLMはメタのみ）
Phase 2  解析   各 raw を意味判定・構造化     → interim/NNN-MM-*.md（1ファイル1セッション）
Phase 3  まとめ URLごとに interim を集約     → stage1-NNN-*.md
Phase 4  レポート stage1 群を横断してまとめ   → report.md
```
> 収集（Phase 1）では、ツールが本文をファイルに保存し LLM にはメタ（パス/タイトル/リンク/プレビュー）
> だけ返すため、複数画面を巡回してもコンテキストが肥大しない。

### 基本実行
```bash
uv run python osint-agent.py --query "Clopマルウェアについて調査してほしい"
```

### オプション
| オプション | 説明 | 例 |
|---|---|---|
| `--query` | 調査クエリ（必須） | `"Clopについて調査"` |
| `--max-urls` | Phase 0 で集めるURL最大数（既定20） | `10` |
| `--resume-dir` | 既存ディレクトリで再開（各フェーズ既存ファイルは自動スキップ） | `surveys/agent-20260618-0937` |
| `--start-from-id` | この ID から開始（それ以前をスキップ） | `20` |
| `--skip-ids` | スキップする ID（カンマ区切り） | `1,3,5` |
| `--plan-only` | **計画のみ**（plan.json だけ） | ─ |
| `--no-report` | **レポート前まで**（計画→収集→解析→まとめ） | ─ |
| `--lm-studio` / `--lm-model` | ローカルLLMで実行 | ─ |

### ユースケース
```bash
# A) まず計画だけ見て対象URLを吟味
uv run python osint-agent.py --query "..." --plan-only
#   → plan.json を確認・編集してから続きを実行

# B) ID 20 以降だけ調査（前回20で中断したケース）
uv run python osint-agent.py --query "..." \
  --resume-dir surveys/agent-20260618-0937 --start-from-id 20
```

---

## 4. osint-survey.py（サイト棚卸し調査）

### フェーズ（4エージェント・パイプライン）
```
Phase 1  概要(計画) トップページ概要収集     → stage1-overview.md, stage1-urls.json
Phase 2  収集      各URLをブラウザ操作で保存  → raw/NNN-MM-*.md（本文はファイルへ）
Phase 3  解析      各 raw を意味判定・構造化  → interim/NNN-MM-*.md
Phase 4  まとめ    URLごとに interim を集約   → stage2-NNN-*.md
Phase 5  レポート  overview + stage2 を横断    → report-trends.md
```

### 基本実行
```bash
uv run python osint-survey.py --site breached-su --url https://breached.su
```

### オプション
| オプション | 説明 | 例 |
|---|---|---|
| `--site` | サイト識別名（出力名に使用・必須） | `breached-su` |
| `--url` | 調査対象URL（Phase 1 必須） | `https://breached.su` |
| `--max-items` | 収集対象の上限件数（0=無制限） | `5` |
| `--resume-dir` | 既存ディレクトリで再開（各フェーズ既存ファイルは自動スキップ） | `surveys/breached-su-20260618-0206` |
| `--skip-ids` | スキップする ID（カンマ区切り） | `2,7,9` |
| `--site-note` | Phase 1 に渡すサイト固有メモ | `"Databasesを重点調査"` |
| `--plan-only` | 概要(計画)のみ | ─ |
| `--no-report` | 詳細処理(収集→解析→まとめ)まで・レポートなし（概要は既存 urls.json を使用） | ─ |
| `--report-only` | レポート生成のみ（既存 stage2 から） | ─ |
| `--lm-studio` / `--lm-model` | ローカルLLMで実行 | ─ |

### ユースケース
```bash
# A) 概要だけ確認 → URL吟味 → 詳細調査
uv run python osint-survey.py --site breached-su --url https://breached.su --plan-only
#   stage1-urls.json を編集してから ↓
uv run python osint-survey.py --site breached-su --resume-dir surveys/... --no-report

# B) 件数を絞って試す
uv run python osint-survey.py --site breached-su --url https://breached.su --max-items 3

# C) レポートだけ作り直す
uv run python osint-survey.py --site breached-su --resume-dir surveys/... --report-only
```

---

## 5. ログイン・CAPTCHA への対応（resume方式・両ツール共通）

ログイン壁や CAPTCHA は **resume 方式**で乗り越えます。ブラウザが共有バックエンドで
常駐しているため、Claude が一度終了してもログイン状態は保持されます。

### 流れ
```
1. Claude がログイン/CAPTCHA を検出 → 即終了（待機しない）
2. オーケストレーターが検知して一時停止し、こう表示する:
     ⏸  ログイン/CAPTCHA が必要です: <URL>
        ブラウザ画面で解決したら Enter を押してください

3. あなたが【ヘッドフルのブラウザ画面】でログイン or CAPTCHA を解決する
4. ターミナルに戻って Enter を押す
5. オーケストレーターが同じセッションを resume
     → Claude が「中断したページの続き」から文脈を復元して調査を再開
```

### ポイント
- **待つ必要なし**: 解決して Enter を押すまで処理は止まって待っている（タイムアウトしない）
- **文脈が保たれる**: どこまで調べたかを覚えたまま再開する
- **スキップしたい時は Ctrl+C**: そのURLを保留して次へ（後で `--resume-dir` で再開可能）
- **パスワードは会話・ターミナルに貼らない**: 必ずブラウザ画面で入力する

### 事前ログインしておく方法（推奨）
調査前に対話セッションでログインを済ませておくと、本番でログイン壁に当たりません。
```bash
claude            # 対話モードで起動
/osint-login-site # ログインスキル → ブラウザ画面でログイン
```
共有ブラウザなので、このログイン状態がそのまま osint-agent/survey に引き継がれます。

---

## 6. 途中再開チートシート

| やりたいこと | コマンド |
|---|---|
| 中断した調査を再開（完了分は自動スキップ） | `--resume-dir surveys/<dir>` |
| 特定 ID から再開（agent のみ） | `--resume-dir ... --start-from-id 20` |
| 特定 ID をスキップ | `--skip-ids 2,7,9` |
| レポートだけ再生成（survey） | `--resume-dir ... --report-only` |
| 計画/概要だけ先に作る | `--plan-only` |
| ローカルLLMで実行 | `--lm-studio` |

> 出力ディレクトリ名は `surveys/agent-<日時>`（agent）/ `surveys/<site>-<日時>`（survey）。
> `--resume-dir` には実際に生成されたディレクトリ名を指定してください。

---

## 7. トラブルシューティング

| 症状 | 対処 |
|---|---|
| プロンプトがLLMに飛ばない | `--lm-studio` を付け忘れていないか確認（API版とローカル版で経路が違う） |
| ログイン壁/CAPTCHAで止まる | §5 の通りブラウザで解決して Enter（正常動作） |
| Phase 1/概要で `not_logged_in` 連発 | 事前に `/osint-login-site` でログイン |
| tor_mcp ポート競合 | `taskkill /IM tor.exe /F`（孤立プロセス除去） |
| バックエンドが応答しない | `tor_mcp/vendor/backend.log` を確認、必要なら §2 で停止して再起動 |
| 日本語出力が文字化け | `PYTHONUTF8=1` を付けて実行 |
