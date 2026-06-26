#!/usr/bin/env python3
# osint-agent.py
# 自然言語クエリからOSINT調査を自律的に実施するエージェント
#
# 使い方:
#   python osint-agent.py --query "Clopについて調査してほしい"
#   python osint-agent.py --query "日本企業へのランサムウェア攻撃" --max-urls 10
#   python osint-agent.py --query "..." --resume-dir surveys/agent-20260618-0300
#   python osint-agent.py --query "..." --resume-dir surveys/agent-... --start-from-id 20
#   python osint-agent.py --query "..." --plan-only    # 計画(plan.json)だけ作る
#   python osint-agent.py --query "..." --no-report    # まとめ(stage1)まで・レポートなし
#
# LM Studio (ローカルLLM) を使う場合:
#   python osint-agent.py --query "..." --lm-studio
#   python osint-agent.py --query "..." --lm-studio --lm-model <モデル名>
#   ※ --lm-studio は Invoke-ClaudeLocal 相当 (ANTHROPIC_BASE_URL=http://localhost:1234)

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

# cp932 端末でも罫線・絵文字（═ ⏸ 等）を落とさず出力する
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── ANSI カラー ───────────────────────────────────────────
class C:
    CYAN    = "\033[96m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    MAGENTA = "\033[95m"
    GRAY    = "\033[90m"
    RESET   = "\033[0m"

def cprint(msg, color=C.RESET):
    print(f"{color}{msg}{C.RESET}", flush=True)

# ── ログイン/CAPTCHA 解決待ち ─────────────────────────────
def wait_for_user_ack(url: str) -> bool:
    """ログイン/CAPTCHA 解決をユーザーに促し、Enter を確実に待つ。

    戻り値 True=解決した(resumeする) / False=待機できない・中断(保留)。
    非対話端末（IDE出力パネル/パイプ等）では input() が素通りして自動再開に
    見えるため、isatty() で判定し、その場合は False を返して保留にする。
    """
    cprint("\n" + "═" * 60, C.YELLOW)
    cprint(f"  ⏸  ログイン/CAPTCHA が必要です: {url}", C.YELLOW)
    cprint("  ブラウザ画面で解決したら Enter（スキップ/中断は Ctrl+C）", C.YELLOW)
    cprint("═" * 60, C.YELLOW)
    if not sys.stdin.isatty():
        cprint("  [警告] 対話端末でないため入力待ちできません。保留します。", C.YELLOW)
        cprint("  ※ 本物のターミナルで実行してください（IDE出力パネル/パイプ不可）。", C.GRAY)
        return False
    try:
        sys.stdout.write("\a")  # ベルで通知
        sys.stdout.flush()
        input("  >> ")
        return True
    except (EOFError, KeyboardInterrupt):
        return False

# deepdarkCTI ディレクトリのパス
DEEPDARK_DIR = Path(__file__).parent / "deepdarkCTI"

def get_deepdark_file_list() -> str:
    """deepdarkCTI のファイル一覧を文字列で返す"""
    if not DEEPDARK_DIR.exists():
        return "(deepdarkCTI ディレクトリが見つかりません)"
    files = sorted(DEEPDARK_DIR.glob("*.md"))
    return "\n".join(f"  - {f.name}" for f in files)

# ── Claude 呼び出し ───────────────────────────────────────
def invoke_claude(prompt: str, label: str, claude_cmd: str,
                  session_id: str = "", resume: bool = False) -> str:
    """claudeを呼び出し、標準出力を返す。エラー時は空文字列。

    session_id を渡すとそのIDでセッションを作成（--session-id）。
    resume=True なら既存セッションを文脈復元して続行（--resume）。
    ログイン/CAPTCHA で中断した調査を、ユーザー解決後に同じ文脈で再開するために使う。
    """
    cprint(f"\n>>> {label}", C.CYAN)
    cprint("─" * 60, C.GRAY)

    # Windows では長い多行プロンプトをコマンドライン引数で渡すと
    # list2cmdline による文字列化で改行・日本語・クォートが壊れるため stdin 経由で渡す
    cmd = claude_cmd.split() + ["--dangerously-skip-permissions", "-p"]
    if session_id:
        cmd += (["--resume", session_id] if resume else ["--session-id", session_id])
    # claude(Node製)が親コンソールの入力モードを変更し、後続の input() を壊すのを防ぐため
    # 親コンソールから隔離する（CREATE_NO_WINDOW）。出力は stdin/stdout のパイプで取得する。
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    output = result.stdout or ""
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        cprint(f"[WARNING] claude が終了コード {result.returncode} を返しました", C.YELLOW)

    print(output, end="", flush=True)
    cprint("─" * 60, C.GRAY)
    return output

# ── パイプライン段の実行（ファイル生成まで1回リトライ） ─────
def invoke_until_file(prompt: str, out_file: Path, label: str, claude_cmd: str) -> bool:
    """claude を呼び、out_file が生成されなければ別セッションで1回だけ再試行する。

    解析・まとめ・レポートの成功判定はファイル生成の有無で行う（claude が exit!=0 や
    ツール呼び出し失敗で終わっても誤って [完了] にしないため）。ブラウザを使わない段で使う。
    """
    invoke_claude(prompt, label, claude_cmd, session_id=str(uuid.uuid4()))
    if not out_file.exists():
        cprint(f"  [再試行] 結果ファイルが未生成（claude エラーの可能性）。もう一度試します...", C.YELLOW)
        invoke_claude(prompt, label + " [retry]", claude_cmd, session_id=str(uuid.uuid4()))
    return out_file.exists()

# ── メイン ────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="OSINT 自律調査エージェント")
    parser.add_argument("--query",       required=True,  help="調査クエリ（例: 'Clopについて調査してほしい'）")
    parser.add_argument("--output-dir",  default="surveys", help="出力ルートディレクトリ")
    parser.add_argument("--resume-dir",  default="",     help="既存の調査ディレクトリを指定して再開")
    parser.add_argument("--max-urls",    type=int, default=20, help="Phase 0 で収集する URL の最大件数 (デフォルト: 20)")
    parser.add_argument("--skip-ids",    default="",     help="Phase 1 でスキップする ID (カンマ区切り: 1,3,5)")
    parser.add_argument("--start-from-id", type=int, default=0, help="Phase 1 をこの ID から開始 (それ以前をスキップ)")
    parser.add_argument("--claude-cmd",  default="claude", help="claude 実行コマンド")
    parser.add_argument("--plan-only", action="store_true", help="計画(plan.json)のみで終了")
    parser.add_argument("--no-report", action="store_true", help="レポート前まで（計画→収集→解析→まとめ）で終了")
    parser.add_argument("--lm-studio",   action="store_true", help="LM Studio経由で実行 (ANTHROPIC_BASE_URL=http://localhost:1234)")
    parser.add_argument("--lm-model",    default="qwen3.6-35b-a3b-uncensored-genesis-v2-apex-mtp", help="LM Studioで使用するモデル名")
    args = parser.parse_args()

    # LM Studio モード: Invoke-ClaudeLocal と同等の環境設定
    if args.lm_studio:
        os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:1234"
        os.environ["CLAUDE_CODE_ATTRIBUTION_HEADER"] = "0"
        if "--model" not in args.claude_cmd:
            args.claude_cmd += f" --model {args.lm_model}"
        cprint(f"[LM Studio] ANTHROPIC_BASE_URL=http://localhost:1234 model={args.lm_model}", C.YELLOW)

    # ── 出力ディレクトリ確定 ────────────────────────────
    if args.resume_dir:
        survey_dir = Path(args.resume_dir)
        if not survey_dir.exists():
            print(f"ERROR: resume-dir が見つかりません: {survey_dir}", file=sys.stderr)
            sys.exit(1)
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M")
        survey_dir = Path(args.output_dir) / f"agent-{ts}"
        survey_dir.mkdir(parents=True, exist_ok=True)

    plan_file   = survey_dir / "plan.json"
    report_file = survey_dir / "report.md"
    skip_ids    = {s.strip() for s in args.skip_ids.split(",") if s.strip()}

    cprint(f"=== OSINT Agent ===", C.MAGENTA)
    cprint(f"クエリ: {args.query}")
    cprint(f"出力先: {survey_dir}")

    # ── Phase 0a: 表層収集（deepdarkCTI + Brave/Tavily） ──
    p0_clearnet = survey_dir / "p0-clearnet.json"
    if not p0_clearnet.exists():
        prompt_0a = f"""/osint-plan-clearnet を使って表層情報を収集してください。

QUERY: {args.query}
DEEPDARK_PATH: {DEEPDARK_DIR.resolve()}
OUTPUT_PATH: {p0_clearnet.resolve()}
MAX_URLS: {args.max_urls}
"""
        if not invoke_until_file(prompt_0a, p0_clearnet, "Phase 0a: 表層収集", args.claude_cmd):
            cprint("[Phase 0a 失敗] p0-clearnet.json が生成されませんでした。続行します。", C.YELLOW)
    else:
        cprint(f"[Phase 0a スキップ] {p0_clearnet} が既に存在します", C.GRAY)

    # ── Phase 0b: 暗層収集（onion_search + フォーラム） ──
    p0_dark = survey_dir / "p0-dark.json"
    if not p0_dark.exists():
        prompt_0b = f"""/osint-plan-dark を使ってダークウェブ・フォーラム情報を収集してください。

QUERY: {args.query}
CLEARNET_PATH: {p0_clearnet.resolve()}
OUTPUT_PATH: {p0_dark.resolve()}
MAX_URLS: {args.max_urls}
"""
        sid = str(uuid.uuid4())
        out = invoke_claude(prompt_0b, "Phase 0b: 暗層収集", args.claude_cmd, session_id=sid)
        auth_hit = ("FORUM_AUTH_REQUIRED" in out or "ERROR: auth_required" in out)

        if not p0_dark.exists() and auth_hit:
            # フォーラムのログイン/CAPTCHA で中断（onion 結果での代替はしない）。
            # ユーザー解決を待ち、同じ文脈で resume して onion+フォーラム両方を書き出させる。
            if wait_for_user_ack("フォーラムのログイン/CAPTCHA"):
                cprint("  解決を確認。同じ文脈で暗層収集を再開します...", C.CYAN)
                invoke_claude(
                    "ログイン/CAPTCHA をユーザーが解決しました。中断したところから収集を続け、"
                    "onion とフォーラム両方の結果を OUTPUT_PATH に書き出してください。",
                    "Phase 0b: 暗層収集 [resume]", args.claude_cmd, session_id=sid, resume=True,
                )
            else:
                cprint("  [中断] フォーラム認証が未解決のため暗層収集を中断しました（onion 結果での代替はしません）。", C.YELLOW)
                cprint(f"  ログイン後: python osint-agent.py --query \"{args.query}\" --resume-dir {survey_dir}", C.MAGENTA)
        elif not p0_dark.exists():
            # 認証以外の理由で未生成（claude エラー等）→ 別セッションで1回だけ再試行。
            cprint("  [再試行] p0-dark.json が未生成。もう一度試します...", C.YELLOW)
            invoke_claude(prompt_0b, "Phase 0b: 暗層収集 [retry]", args.claude_cmd, session_id=str(uuid.uuid4()))

        if not p0_dark.exists():
            cprint("[Phase 0b 失敗] p0-dark.json が生成されませんでした。", C.YELLOW)
    else:
        cprint(f"[Phase 0b スキップ] {p0_dark} が既に存在します", C.GRAY)

    # ── Phase 0c: 計画統合（収集結果 → plan.json） ──
    if not plan_file.exists():
        sources = []
        if p0_clearnet.exists():
            sources.append(str(p0_clearnet.resolve()))
        if p0_dark.exists():
            sources.append(str(p0_dark.resolve()))

        if not sources:
            print("ERROR: Phase 0a/0b の結果ファイルがいずれも存在しません。", file=sys.stderr)
            sys.exit(1)

        source_list = "\n".join(f"  - {s}" for s in sources)
        prompt_0c = f"""以下の収集結果ファイルを読み込み、plan.json を作成してください。

クエリ: {args.query}
収集結果ファイル:
{source_list}
出力先: {plan_file.resolve()}
最大件数: {args.max_urls}

## 手順
1. 各ファイルを Read で読み込む
2. 全 URL を統合し重複を除外する
3. クエリへの関連度・情報の具体性・新鮮さでランク付けし上位 {args.max_urls} 件を選定
4. 以下の形式で plan.json に保存する:

```json
[
  {{
    "id": 1,
    "url": "https://example.com/thread/12345",
    "category": "Ransomware / Clop",
    "title": "スレッドタイトルまたはページタイトル",
    "source": "tor|clearnet|forum",
    "reason": "このURLを選定した理由（クエリとの関連性）",
    "priority": "high|medium|low"
  }}
]
```

id は 1 から連番を振る。書き込んだら URL 件数と主な発見を 1〜3 行でサマリーとして出力してください。
"""
        invoke_until_file(prompt_0c, plan_file, "Phase 0c: 計画統合", args.claude_cmd)

        if not plan_file.exists():
            print(f"ERROR: Phase 0c が正常に完了しませんでした。{plan_file} が存在しません。", file=sys.stderr)
            sys.exit(1)

        cprint(f"[Phase 0 完了] {plan_file}", C.GREEN)
    else:
        cprint(f"[Phase 0 スキップ] {plan_file} が既に存在します", C.GRAY)

    if args.plan_only:
        cprint("=== plan-only モード: 計画のみで終了 ===", C.YELLOW)
        sys.exit(0)

    # ── 調査計画 (plan.json) を読み込む（P1〜P3 共通） ──
    try:
        urls = json.loads(plan_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: plan.json の読み込みに失敗しました: {e}", file=sys.stderr)
        sys.exit(1)

    raw_dir     = survey_dir / "raw"
    interim_dir = survey_dir / "interim"

    def in_scope(it) -> bool:
        ids = str(it["id"])
        return not (ids in skip_ids or (args.start_from_id > 0 and it["id"] < args.start_from_id))

    items = [it for it in urls if in_scope(it)]

    def pad(it) -> str:
        return str(it["id"]).zfill(3)

    def slug_of(it) -> str:
        s = re.sub(r"[^a-zA-Z0-9]", "-", it["url"])
        return re.sub(r"-+", "-", s).strip("-")[:40]

    # ── Phase 1: 収集（ブラウザ操作 → raw 保存。LLMは収集コマンド発行のみ） ──
    cprint(f"\n=== Phase 1: 収集 ({len(items)} 件) ===", C.MAGENTA)
    raw_dir.mkdir(parents=True, exist_ok=True)

    # 前回保留したログイン待ちURLをキュー先頭に戻す
    pending_file = survey_dir / "pending-login.json"
    if pending_file.exists():
        try:
            pending = json.loads(pending_file.read_text(encoding="utf-8"))
            pids = {p["id"] for p in pending}
            items = [p for p in pending if in_scope(p)] + [u for u in items if u["id"] not in pids]
            cprint(f"[再開] {len(pending)} 件の保留URLをキュー先頭に戻しました", C.YELLOW)
            pending_file.unlink()
        except Exception:
            pass

    pending_login = []
    for i, item in enumerate(items, 1):
        pid = pad(item)
        if list(raw_dir.glob(f"{pid}-*.md")):
            cprint(f"  [EXIST] 収集 {i}/{len(items)} : {item.get('title', item['url'])}", C.GRAY)
            continue
        source_note = {
            "tor":      "Tor ブラウザ（browser_open）経由でアクセス。",
            "clearnet": "通常のブラウザ（browser_open）でアクセス。",
            "forum":    "ログイン済みブラウザセッションを使用。",
        }.get(item.get("source", "clearnet"), "")

        collect_prompt = f"""/osint-collect を使って収集してください。

調査URL: {item['url']}
URL id: {pid}
保存先ディレクトリ: {raw_dir.resolve()}
ファイル名規則: {pid}-MM-<slug>.md （MM=画面連番 01,02,…）
アクセス方法: {source_note}
調査目的: {args.query}

各ツールは必ず save_path 付きで呼び、本文はファイルに保存してください（解析しない）。
ログイン/CAPTCHA が現れたら ERROR: auth_required を出して終了してください。
"""
        label = f"P1収集 [{i}/{len(items)}] {item.get('title', item['url'])}"
        sid = str(uuid.uuid4())
        out = invoke_claude(collect_prompt, label, args.claude_cmd, session_id=sid)

        if "ERROR: auth_required" in out or "ERROR: not_logged_in" in out:
            if not wait_for_user_ack(item['url']):
                cprint(f"\n  [保留] {item['url']} を保留します。", C.YELLOW)
                pending_login.append(item)
                cprint(f"  再開: python osint-agent.py --query \"{args.query}\" --resume-dir {survey_dir}", C.MAGENTA)
                break
            cprint(f"  解決を確認。同じ文脈で収集を再開します...", C.CYAN)
            out = invoke_claude(
                "ログイン/CAPTCHA をユーザーが解決しました。中断したところから収集を続けてください。",
                label + " [resume]", args.claude_cmd, session_id=sid, resume=True,
            )
            if "ERROR: auth_required" in out or "ERROR: not_logged_in" in out:
                cprint(f"  [保留] 再開後も認証エラー: {item['url']}", C.YELLOW)
                pending_login.append(item)
                continue

        if not list(raw_dir.glob(f"{pid}-*.md")):
            cprint(f"  [再試行] 収集ファイルが未生成。もう一度試します...", C.YELLOW)
            invoke_claude(collect_prompt, label + " [retry]", args.claude_cmd, session_id=str(uuid.uuid4()))
        n = len(list(raw_dir.glob(f"{pid}-*.md")))
        if n:
            cprint(f"  [完了] 収集 {n} 画面 → {raw_dir}/{pid}-*.md", C.GREEN)
        else:
            cprint(f"  [失敗] 収集できませんでした: {item['url']}（resume-dir で再実行可）", C.YELLOW)

    if pending_login:
        pending_file.write_text(json.dumps(pending_login, ensure_ascii=False, indent=2), encoding="utf-8")
        cprint(f"\n  [保留リスト保存] {len(pending_login)} 件 → {pending_file}", C.YELLOW)
        cprint(f"  ログイン後: python osint-agent.py --query \"{args.query}\" --resume-dir {survey_dir}", C.MAGENTA)

    cprint("[Phase 1 収集 完了]", C.GREEN)

    # ── Phase 2: 解析（raw → interim。1ファイル1セッション・ブラウザ不要） ──
    interim_dir.mkdir(parents=True, exist_ok=True)
    raws = sorted(raw_dir.glob("*.md"))
    cprint(f"\n=== Phase 2: 解析 ({len(raws)} ファイル) ===", C.MAGENTA)
    for i, raw in enumerate(raws, 1):
        interim = interim_dir / raw.name
        if interim.exists():
            cprint(f"  [EXIST] 解析 {i}/{len(raws)} : {raw.name}", C.GRAY)
            continue
        analyze_prompt = f"""/osint-analyze を使って解析してください。

入力 raw ファイル: {raw.resolve()}
出力先 interim ファイル: {interim.resolve()}
調査目的: {args.query}
"""
        if invoke_until_file(analyze_prompt, interim, f"P2解析 [{i}/{len(raws)}] {raw.name}", args.claude_cmd):
            cprint(f"  [完了] → {interim}", C.GREEN)
        else:
            cprint(f"  [失敗] 解析できませんでした: {raw.name}", C.YELLOW)

    cprint("[Phase 2 解析 完了]", C.GREEN)

    # ── Phase 3: まとめ（interim → stage1。URL単位で集約） ──
    cprint(f"\n=== Phase 3: まとめ ({len(items)} 件) ===", C.MAGENTA)
    for i, item in enumerate(items, 1):
        pid = pad(item)
        stage1 = survey_dir / f"stage1-{pid}-{slug_of(item)}.md"
        if stage1.exists():
            cprint(f"  [EXIST] まとめ {i}/{len(items)} : {item.get('title', item['url'])}", C.GRAY)
            continue
        my_interims = sorted(interim_dir.glob(f"{pid}-*.md"))
        if not my_interims:
            cprint(f"  [SKIP] まとめ {i}/{len(items)} : interim 無し（収集失敗）: {item['url']}", C.GRAY)
            continue
        interim_list = "\n".join(f"  - {f.resolve()}" for f in my_interims)
        summarize_prompt = f"""/osint-summarize を使ってまとめてください。

対象 interim ファイル:
{interim_list}
出力先 stage1 ファイル: {stage1.resolve()}
調査URL: {item['url']}
カテゴリ: {item.get('category', '')}
タイトル: {item.get('title', '')}
調査目的: {args.query}
"""
        label = f"P3まとめ [{i}/{len(items)}] {item.get('title', item['url'])}"
        if invoke_until_file(summarize_prompt, stage1, label, args.claude_cmd):
            cprint(f"  [完了] → {stage1}", C.GREEN)
        else:
            cprint(f"  [失敗] まとめできませんでした: {item['url']}", C.YELLOW)

    cprint("[Phase 3 まとめ 完了]", C.GREEN)

    if args.no_report:
        cprint("=== no-report モード: レポート前で終了 ===", C.YELLOW)
        sys.exit(0)

    # ── Phase 4: レポート生成（stage1 群 → report） ──
    stage1_files = sorted(survey_dir.glob("stage1-*.md"))
    stage1_list  = "\n".join(f"  - {f}" for f in stage1_files)

    p4_prompt = f"""/osint-site-report を使ってレポートを生成してください。

## 調査情報
元クエリ: {args.query}
調査ディレクトリ: {survey_dir}
調査計画: {plan_file}
出力先: {report_file}

## 読み込むファイル
調査計画 (plan.json) と以下の詳細調査結果(stage1)を全て読み込んでください:
{stage1_list}

## レポート要件
- クエリ「{args.query}」に対する回答として構成する
- 調査した情報源（Tor・表層ウェブ・フォーラム）を区別して記載する
- 脅威インテリジェンスとして有用な情報を優先する
- アクセス不可・認証エラーだったURLも記録する

osint-site-report スキルの出力形式に従ってください。
"""
    invoke_claude(p4_prompt, "Phase 4: レポート生成", args.claude_cmd)
    if report_file.exists():
        cprint(f"[Phase 4 完了] {report_file}", C.GREEN)
    else:
        cprint(f"[Phase 4 失敗] レポートが生成されませんでした（claude エラーの可能性）。", C.YELLOW)
        cprint(f"  resume-dir で再実行するとレポート生成だけやり直せます: {report_file}", C.GRAY)

    cprint(f"\n=== 調査完了 ===", C.MAGENTA)
    cprint(f"出力先: {survey_dir}")

if __name__ == "__main__":
    main()
