#!/usr/bin/env python3
# osint-survey.py
# BreachForums等ログイン済みサイトをフェーズ分割で調査するオーケストレーター
#
# 使い方:
#   python osint-survey.py --site breached-su --url https://breached.su
#   python osint-survey.py --site breached-su --url https://breached.su --max-items 5
#   python osint-survey.py --site breached-su --resume-dir surveys/breached-su-20260618-0030
#   python osint-survey.py --site breached-su --resume-dir surveys/... --no-report
#   python osint-survey.py --site breached-su --resume-dir surveys/... --report-only
#
# LM Studio (ローカルLLM) を使う場合:
#   python osint-survey.py --site breached-su --url https://breached.su --lm-studio
#   python osint-survey.py --site breached-su --url https://breached.su --lm-studio --lm-model <モデル名>
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
    if "ERROR: not_logged_in" in output or "ERROR: auth_required" in output:
        cprint(f"[AUTH ERROR] ログイン切れまたは認証ブロックが発生しました", C.YELLOW)
        cprint(output.strip(), C.YELLOW)

    print(output, end="", flush=True)
    cprint("─" * 60, C.GRAY)
    return output

# ── パイプライン段の実行（ファイル生成まで1回リトライ） ─────
def invoke_until_file(prompt: str, out_file: Path, label: str, claude_cmd: str) -> bool:
    """claude を呼び、out_file が生成されなければ別セッションで1回だけ再試行する。

    解析・まとめ・レポートの成功判定はファイル生成の有無で行う。ブラウザを使わない段で使う。
    """
    invoke_claude(prompt, label, claude_cmd, session_id=str(uuid.uuid4()))
    if not out_file.exists():
        cprint(f"  [再試行] 結果ファイルが未生成（claude エラーの可能性）。もう一度試します...", C.YELLOW)
        invoke_claude(prompt, label + " [retry]", claude_cmd, session_id=str(uuid.uuid4()))
    return out_file.exists()

# ── メイン ────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="OSINT サイト調査オーケストレーター")
    parser.add_argument("--site",        required=True,  help="サイト識別名 (例: breached-su)")
    parser.add_argument("--url",         default="",     help="調査対象URL (Phase1 必須)")
    parser.add_argument("--output-dir",  default="surveys", help="出力ルートディレクトリ")
    parser.add_argument("--resume-dir",  default="",     help="既存の調査ディレクトリを指定して再開")
    parser.add_argument("--skip-ids",    default="",     help="Phase2でスキップするID (カンマ区切り: 1,3,5)")
    parser.add_argument("--max-items",   type=int, default=0, help="Phase2の上限件数 (0=無制限)")
    parser.add_argument("--site-note",   default="",     help="サイト固有メモ (Phase1に渡す)")
    parser.add_argument("--claude-cmd",  default="claude", help="claude実行コマンド")
    parser.add_argument("--plan-only",   action="store_true", help="概要収集(計画)のみで終了")
    parser.add_argument("--no-report",   action="store_true", help="詳細処理(収集→解析→まとめ)まで・レポートなし（概要は既存 urls.json を使用）")
    parser.add_argument("--report-only", action="store_true", help="レポート生成のみ（既存の stage2 から）")
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
        survey_dir = Path(args.output_dir) / f"{args.site}-{ts}"
        survey_dir.mkdir(parents=True, exist_ok=True)

    overview_file = survey_dir / "stage1-overview.md"
    urls_file     = survey_dir / "stage1-urls.json"
    report_file   = survey_dir / "report-trends.md"
    skip_ids      = {s.strip() for s in args.skip_ids.split(",") if s.strip()}

    cprint(f"=== OSINT Survey: {args.site} ===", C.MAGENTA)
    cprint(f"出力先: {survey_dir}")

    # ── Phase 1: 概要収集 ────────────────────────────────
    # resume-dir で既に stage1-urls.json があれば概要を作り直さず Phase2 以降へ進む
    # （作り直すと id 採番が変わり Phase2〜4 の [EXIST] 判定がズレて全件やり直しになる）
    if urls_file.exists():
        cprint(f"[Phase 1 スキップ] 既存の概要を使用: {urls_file}", C.GRAY)
    if not args.no_report and not args.report_only and not urls_file.exists():
        if not args.url:
            print("ERROR: Phase 1 には --url が必要です", file=sys.stderr)
            sys.exit(1)

        note_line = f"\nサイト固有メモ: {args.site_note}" if args.site_note else ""
        p1_prompt = f"""/osint-site-overview を使って調査してください。

対象URL: {args.url}
出力先(overview): {overview_file}
出力先(urls.json): {urls_file}{note_line}

ブラウザにすでにログイン済みの場合はそのまま利用してください。
未ログインの場合は調査を中断し、ログインが必要である旨を報告してください。
"""
        p1_sid = str(uuid.uuid4())
        out = invoke_claude(p1_prompt, f"Phase 1: 概要収集 ({args.url})", args.claude_cmd, session_id=p1_sid)

        if "ERROR: not_logged_in" in out or "ERROR: auth_required" in out:
            # Claude はログイン/CAPTCHA を検出して終了。ブラウザは共有バックエンドで常駐して
            # いるので、ユーザーが画面で解決 → Enter → 同じセッションを resume して続行する。
            if not wait_for_user_ack(args.url):
                cprint("\n[ABORT] 認証待ちを中断しました。ブラウザでログインしてから再実行してください。", C.YELLOW)
                sys.exit(2)
            cprint("  解決を確認。同じ文脈で概要収集を再開します...", C.CYAN)
            out = invoke_claude(
                "ログイン/CAPTCHA をユーザーが解決しました。中断したところから概要収集を続けてください。",
                "Phase 1: 概要収集 [resume]", args.claude_cmd, session_id=p1_sid, resume=True,
            )
            if "ERROR: not_logged_in" in out or "ERROR: auth_required" in out:
                cprint("\n[ABORT] 再開後も認証エラー。ブラウザでログインしてから再実行してください。", C.YELLOW)
                sys.exit(2)

        if not urls_file.exists():
            print(f"ERROR: Phase 1 が正常に完了しませんでした。{urls_file} が存在しません。", file=sys.stderr)
            sys.exit(1)

        cprint(f"[Phase 1 完了] {overview_file} / {urls_file}", C.GREEN)

        if args.plan_only:
            cprint("=== plan-only モード: 概要のみで終了 ===", C.YELLOW)
            sys.exit(0)

    # ── Phase 2〜4: 収集 → 解析 → まとめ ──────────────────
    if not args.plan_only and not args.report_only:
        if not urls_file.exists():
            print(f"ERROR: {urls_file} が見つかりません。先に Phase 1 を実行してください。", file=sys.stderr)
            sys.exit(1)

        urls = json.loads(urls_file.read_text(encoding="utf-8"))
        if args.max_items > 0:
            urls = urls[:args.max_items]
        items = [it for it in urls if str(it["id"]) not in skip_ids]

        raw_dir     = survey_dir / "raw"
        interim_dir = survey_dir / "interim"
        raw_dir.mkdir(parents=True, exist_ok=True)
        interim_dir.mkdir(parents=True, exist_ok=True)

        def pad(it):
            return str(it["id"]).zfill(3)

        def slug_of(it):
            s = re.sub(r"[^a-zA-Z0-9]", "-", it["url"])
            return re.sub(r"-+", "-", s).strip("-")[:40]

        # ── Phase 2: 収集（ブラウザ操作 → raw 保存。LLMは収集コマンド発行のみ） ──
        cprint(f"\n=== Phase 2: 収集 ({len(items)} 件) ===", C.MAGENTA)
        for i, item in enumerate(items, 1):
            pid = pad(item)
            if list(raw_dir.glob(f"{pid}-*.md")):
                cprint(f"  [EXIST] 収集 {i}/{len(items)} : {item['title']}", C.GRAY)
                continue
            collect_prompt = f"""/osint-collect を使って収集してください。

調査URL: {item['url']}
URL id: {pid}
保存先ディレクトリ: {raw_dir.resolve()}
ファイル名規則: {pid}-MM-<slug>.md （MM=画面連番 01,02,…）
カテゴリ: {item['category']}
調査目的: {args.site} の動向把握（{item['title']}）

各ツールは必ず save_path 付きで呼び、本文はファイルに保存してください（解析しない）。
ログイン/CAPTCHA が現れたら ERROR: auth_required を出して終了してください。
"""
            label = f"P2収集 [{i}/{len(items)}] {item['title']}"
            sid = str(uuid.uuid4())
            out = invoke_claude(collect_prompt, label, args.claude_cmd, session_id=sid)
            if "ERROR: auth_required" in out or "ERROR: not_logged_in" in out:
                if not wait_for_user_ack(item['url']):
                    cprint("  [中断] 認証待ちを中断しました。収集を終了します。", C.YELLOW)
                    break
                cprint("  解決を確認。同じ文脈で収集を再開します...", C.CYAN)
                out = invoke_claude(
                    "ログイン/CAPTCHA をユーザーが解決しました。中断したところから収集を続けてください。",
                    label + " [resume]", args.claude_cmd, session_id=sid, resume=True,
                )
                if "ERROR: auth_required" in out or "ERROR: not_logged_in" in out:
                    cprint(f"  [SKIP] 再開後も認証エラー: {item['url']}", C.YELLOW)
                    continue
            if not list(raw_dir.glob(f"{pid}-*.md")):
                cprint(f"  [再試行] 収集ファイルが未生成。もう一度試します...", C.YELLOW)
                invoke_claude(collect_prompt, label + " [retry]", args.claude_cmd, session_id=str(uuid.uuid4()))
            n = len(list(raw_dir.glob(f"{pid}-*.md")))
            if n:
                cprint(f"  [完了] 収集 {n} 画面 → {raw_dir}/{pid}-*.md", C.GREEN)
            else:
                cprint(f"  [失敗] 収集できませんでした: {item['url']}（resume-dir で再実行可）", C.YELLOW)
        cprint("[Phase 2 収集 完了]", C.GREEN)

        # ── Phase 3: 解析（raw → interim。1ファイル1セッション・ブラウザ不要） ──
        raws = sorted(raw_dir.glob("*.md"))
        cprint(f"\n=== Phase 3: 解析 ({len(raws)} ファイル) ===", C.MAGENTA)
        for i, raw in enumerate(raws, 1):
            interim = interim_dir / raw.name
            if interim.exists():
                cprint(f"  [EXIST] 解析 {i}/{len(raws)} : {raw.name}", C.GRAY)
                continue
            analyze_prompt = f"""/osint-analyze を使って解析してください。

入力 raw ファイル: {raw.resolve()}
出力先 interim ファイル: {interim.resolve()}
調査目的: {args.site} の動向把握
"""
            if invoke_until_file(analyze_prompt, interim, f"P3解析 [{i}/{len(raws)}] {raw.name}", args.claude_cmd):
                cprint(f"  [完了] → {interim}", C.GREEN)
            else:
                cprint(f"  [失敗] 解析できませんでした: {raw.name}", C.YELLOW)
        cprint("[Phase 3 解析 完了]", C.GREEN)

        # ── Phase 4: まとめ（interim → stage2。URL単位で集約） ──
        cprint(f"\n=== Phase 4: まとめ ({len(items)} 件) ===", C.MAGENTA)
        for i, item in enumerate(items, 1):
            pid = pad(item)
            stage2 = survey_dir / f"stage2-{pid}-{slug_of(item)}.md"
            if stage2.exists():
                cprint(f"  [EXIST] まとめ {i}/{len(items)} : {item['title']}", C.GRAY)
                continue
            my_interims = sorted(interim_dir.glob(f"{pid}-*.md"))
            if not my_interims:
                cprint(f"  [SKIP] まとめ {i}/{len(items)} : interim 無し（収集失敗）: {item['url']}", C.GRAY)
                continue
            interim_list = "\n".join(f"  - {f.resolve()}" for f in my_interims)
            summarize_prompt = f"""/osint-summarize を使ってまとめてください。

対象 interim ファイル:
{interim_list}
出力先ファイル: {stage2.resolve()}
調査URL: {item['url']}
カテゴリ: {item['category']}
タイトル: {item['title']}
調査目的: {args.site} の動向把握
"""
            label = f"P4まとめ [{i}/{len(items)}] {item['title']}"
            if invoke_until_file(summarize_prompt, stage2, label, args.claude_cmd):
                cprint(f"  [完了] → {stage2}", C.GREEN)
            else:
                cprint(f"  [失敗] まとめできませんでした: {item['url']}", C.YELLOW)
        cprint("[Phase 4 まとめ 完了]", C.GREEN)

        if args.no_report:
            cprint("=== no-report モード: レポート前で終了 ===", C.YELLOW)
            sys.exit(0)

    # ── Phase 5: 動向レポート生成 ────────────────────────
    if not args.plan_only and not args.no_report:
        p5_prompt = f"""/osint-site-report を使ってまとめてください。

調査ディレクトリ: {survey_dir}
サイト名: {args.site}
出力先: {report_file}

stage1-overview.md と stage2-*.md を全て読み込み、動向レポートを作成してください。
"""
        invoke_claude(p5_prompt, "Phase 5: 動向レポート生成", args.claude_cmd)
        if report_file.exists():
            cprint(f"[Phase 5 完了] {report_file}", C.GREEN)
        else:
            cprint(f"[Phase 5 失敗] レポートが生成されませんでした（claude エラーの可能性）。", C.YELLOW)
            cprint(f"  resume-dir --report-only で再実行できます: {report_file}", C.GRAY)

    cprint(f"\n=== 調査完了 ===", C.MAGENTA)
    cprint(f"出力先: {survey_dir}")

if __name__ == "__main__":
    main()
