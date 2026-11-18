import os
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import json
from datetime import datetime, timezone, timedelta

TARGET_LABEL = "auto-fix"
RUNNING_LABEL = "antigravity-wip"
DONE_LABEL = "antigravity-done"
FAILED_LABEL = "antigravity-failed"
MAX_ITEMS_PER_RUN = 5
BOT_COMMENT_MARKER = "<!-- antigravity:bot -->"
MAX_COMMENT_PAGES = 20

# --- 自律お掃除（Auto-Improve）＆ AI査読官設定 ---
ENABLE_AUTO_IMPROVE = True
MAX_AUTO_IMPROVE_PER_DAY = 2       # 1日あたりに自動作成するDraft PRの最大数
ENABLE_AI_PR_REVIEW = True         # PR作成時にAI査読官による自動差分レビューコメントを投稿する
REVIEW_MODELS = ["gemini-4-hydrogen", "gemini-pro-latest"]  # 左から順に試行し、通った最強モデルで査読する
SELF_REPO_NAME = "antigravity-commander"
GLOBAL_RULES_FILE = "GLOBAL_AGENTS.md"
AUTO_IMPROVE_TRACKER_TITLE = "[State] Auto-Improve Tracker"
JST = timezone(timedelta(hours=9))


def sanitize(text: str, *secrets: str) -> str:
    res = str(text)
    for s in secrets:
        if s and len(s) > 4:
            res = res.replace(s, "***")
    return res


def load_global_rules() -> str:
    """司令塔リポジトリ内の GLOBAL_AGENTS.md を読み込み、全リポジトリ共通ルールとして返す"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    rules_path = os.path.join(script_dir, GLOBAL_RULES_FILE)
    if os.path.exists(rules_path):
        try:
            with open(rules_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                return f"\n[GLOBAL ENGINEERING CONSTITUTION]\n{content[:2500]}\n"
        except Exception:
            pass
    return ""


def http_json(url: str, headers: dict, method: str = "GET", data: dict = None, retries: int = 3):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req_headers = dict(headers)
    if body is not None:
        req_headers["Content-Type"] = "application/json"

    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                if res.status == 204:
                    return None
                raw = res.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code} ({url}): {err_body}") from e
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"URL Error ({url}): {e.reason}") from e


def github_api(url: str, token: str, method: str = "GET", data: dict = None, retries: int = 3):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "antigravity-commander",
    }
    return http_json(url, headers, method=method, data=data, retries=retries)


def gemini_api(path: str, api_key: str, method: str = "GET", data: dict = None):
    url = f"https://generativelanguage.googleapis.com/v1beta/{path}"
    return http_json(url, {"x-goog-api-key": api_key}, method=method, data=data)


def get_authenticated_user(token: str) -> str:
    return github_api("https://api.github.com/user", token)["login"]


def find_items_by_label(owner: str, label: str, token: str) -> list:
    items = []
    for item_type in ("is:issue", "is:pull-request"):
        query = urllib.parse.quote(f'user:{owner} {item_type} is:open label:"{label}"')
        url = f"https://api.github.com/search/issues?q={query}&sort=updated&order=asc&per_page={MAX_ITEMS_PER_RUN}"
        res = github_api(url, token) or {}
        items.extend(res.get("items", []))
    items.sort(key=lambda x: x.get("updated_at") or "")
    return items[:MAX_ITEMS_PER_RUN]


def _extract_label_names(labels: list | None) -> set[str]:
    names: set[str] = set()
    for lb in labels or []:
        if isinstance(lb, dict) and lb.get("name"):
            names.add(str(lb["name"]))
        elif isinstance(lb, str) and lb:
            names.add(lb)
    return names


def set_exclusive_label(
    repo_full_name: str,
    number: int,
    add_label: str,
    token: str,
    current_labels: list | None = None,
):
    existing_names = _extract_label_names(current_labels) if current_labels is not None else None

    for old_label in (TARGET_LABEL, RUNNING_LABEL, DONE_LABEL, FAILED_LABEL):
        if old_label == add_label:
            continue
        if existing_names is not None and old_label not in existing_names:
            continue
        try:
            enc = urllib.parse.quote(old_label)
            github_api(
                f"https://api.github.com/repos/{repo_full_name}/issues/{number}/labels/{enc}",
                token,
                method="DELETE",
                retries=1,
            )
        except Exception:
            pass

    if add_label and (existing_names is None or add_label not in existing_names):
        github_api(
            f"https://api.github.com/repos/{repo_full_name}/issues/{number}/labels",
            token,
            method="POST",
            data={"labels": [add_label]},
        )


def post_comment(repo_full_name: str, number: int, body: str, token: str, mark_as_bot: bool = True):
    final_body = body
    if mark_as_bot and BOT_COMMENT_MARKER not in final_body:
        final_body = f"{final_body.rstrip()}\n{BOT_COMMENT_MARKER}"
    github_api(
        f"https://api.github.com/repos/{repo_full_name}/issues/{number}/comments",
        token,
        method="POST",
        data={"body": final_body},
    )


def get_all_issue_comments(repo_full_name: str, number: int, token: str) -> list:
    all_comments: list = []
    for page in range(1, MAX_COMMENT_PAGES + 1):
        batch = github_api(
            f"https://api.github.com/repos/{repo_full_name}/issues/{number}/comments?per_page=100&page={page}",
            token,
        ) or []
        if not isinstance(batch, list) or not batch:
            break
        all_comments.extend(batch)
        if len(batch) < 100:
            break
    return all_comments


def get_branch_sha(repo_full_name: str, branch: str, token: str) -> str:
    try:
        enc_branch = urllib.parse.quote(branch, safe="")
        res = github_api(
            f"https://api.github.com/repos/{repo_full_name}/git/ref/heads/{enc_branch}",
            token,
            retries=1,
        ) or {}
        return (res.get("object") or {}).get("sha") or ""
    except Exception:
        return ""


def has_commits_ahead(repo_full_name: str, base_branch: str, head_branch: str, token: str) -> bool:
    try:
        enc_base = urllib.parse.quote(base_branch, safe="")
        enc_head = urllib.parse.quote(head_branch, safe="")
        res = github_api(
            f"https://api.github.com/repos/{repo_full_name}/compare/{enc_base}...{enc_head}",
            token,
            retries=1,
        ) or {}
        return (res.get("ahead_by") or 0) > 0
    except Exception:
        return False


def review_pull_request(repo_full_name: str, pr_number: int, gemini_key: str, github_token: str):
    """作成・更新されたPRの差分を取得し、AI査読官によるレビューコメントを自動投稿する"""
    if not ENABLE_AI_PR_REVIEW or not pr_number:
        return

    try:
        files_data = github_api(
            f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/files?per_page=30",
            github_token,
        ) or []
        if not files_data:
            return

        diff_chunks = []
        total_len = 0
        for f in files_data:
            if not isinstance(f, dict):
                continue
            fname = f.get("filename", "")
            patch = f.get("patch") or ""
            chunk = f"--- {fname} (+{f.get('additions', 0)} / -{f.get('deletions', 0)}) ---\n{patch}\n"
            if total_len + len(chunk) > 12000:
                diff_chunks.append("... (以降差分省略)")
                break
            diff_chunks.append(chunk)
            total_len += len(chunk)

        diff_text = "\n".join(diff_chunks)
        global_rules = load_global_rules()

        review_prompt = f"""あなたは厳格かつ建設的なシニアソフトウェアエンジニア（コード査読官）です。
以下のGitHub Pull Requestの差分（git diff）をレビューし、リポジトリオーナーがスマホで10秒でマージ判断できる簡潔な日本語レポートを作成してください。
{global_rules}
[対象PR: {repo_full_name} #{pr_number}]
{diff_text}

出力フォーマット（厳守・余計な挨拶不要）：
### 🔍 AI査読官レポート
- **判定:** `🟢 マージ推奨` / `🟡 要目視確認` / `🔴 修正推奨` のいずれか1つ
- **変更の核心:** 何をどう変えたか（1〜2文）
- **リスク・懸念点:** 副作用、エッジケース漏れ、破壊的変更の有無（問題なければ「特になし」）
"""
        payload = {
            "contents": [{"parts": [{"text": review_prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }

        review_text = ""
        used_model = ""
        for model_name in REVIEW_MODELS:
            try:
                res = gemini_api(f"models/{model_name}:generateContent", gemini_key, method="POST", data=payload) or {}
                candidates = res.get("candidates") or []
                parts = (((candidates[0] if candidates else {}).get("content") or {}).get("parts") or [])
                review_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
                if review_text:
                    used_model = model_name
                    break
            except Exception as model_err:
                safe_m_err = sanitize(str(model_err), gemini_key, github_token)[:120]
                print(f"モデル {model_name} での査読をスキップして次を試行します: {safe_m_err}")

        if review_text:
            safe_review = sanitize(review_text, gemini_key, github_token)
            footer = f"\n\n<sub>Reviewed by `{used_model}`</sub>"
            post_comment(repo_full_name, pr_number, safe_review + footer, github_token)
            print(f"🔍 AI査読コメント投稿完了 ({used_model}): {repo_full_name} #{pr_number}")

    except Exception as e:
        safe_err = sanitize(str(e), gemini_key, github_token)[:200]
        print(f"AI査読スキップ ({repo_full_name} #{pr_number}): {safe_err}", file=sys.stderr)


def ensure_pr_exists(
    repo_full_name: str,
    owner: str,
    base_branch: str,
    head_branch: str,
    issue_num: int,
    issue_title: str,
    summary: str,
    token: str,
    draft: bool = False,
) -> tuple[str, int]:
    """PRが存在すれば(html_url, pr_number)を返し、無ければ新規作成して返す"""
    enc_head = urllib.parse.quote(f"{owner}:{head_branch}")
    prs = github_api(
        f"https://api.github.com/repos/{repo_full_name}/pulls?head={enc_head}&state=open",
        token,
    ) or []
    if prs and isinstance(prs, list) and prs[0].get("html_url"):
        return prs[0]["html_url"], prs[0].get("number") or 0

    if issue_num > 0:
        pr_title = f"fix: {issue_title} (#{issue_num})"
        pr_body = f"Closes #{issue_num}\n\n### 自動修正内容\n{summary}"
    else:
        pr_title = f"chore: {issue_title}"
        pr_body = f"### 🤖 自律改善パトロールによる提案 (Draft)\n{summary}\n\n---\n※ 内容をレビューし、問題なければ「Ready for review」にしてマージしてください。"

    created = github_api(
        f"https://api.github.com/repos/{repo_full_name}/pulls",
        token,
        method="POST",
        data={
            "title": pr_title,
            "head": head_branch,
            "base": base_branch,
            "body": pr_body,
            "draft": draft,
        },
    ) or {}
    return created.get("html_url") or "", created.get("number") or 0


def get_pr_context(repo_full_name: str, number: int, owner: str, token: str) -> tuple[str, str]:
    files_data = github_api(
        f"https://api.github.com/repos/{repo_full_name}/pulls/{number}/files?per_page=50",
        token,
    ) or []
    changed_files = [
        f"- `{f.get('filename')}` (+{f.get('additions', 0)} / -{f.get('deletions', 0)})"
        for f in files_data
        if isinstance(f, dict) and f.get("filename")
    ]
    files_summary = "\n".join(changed_files) if changed_files else "（変更ファイル情報なし）"

    timed_feedbacks = []
    comments = get_all_issue_comments(repo_full_name, number, token)
    for c in comments:
        if not isinstance(c, dict):
            continue
        body = (c.get("body") or "").strip()
        user_login = (c.get("user") or {}).get("login")
        if (
            user_login == owner
            and body
            and BOT_COMMENT_MARKER not in body
            and not body.startswith(("✅ 自動", "⚠️ 自動", "⏳ 自動", "### 🔍 AI査読官レポート"))
        ):
            ts = c.get("updated_at") or c.get("created_at") or ""
            timed_feedbacks.append((ts, body))

    try:
        review_comments = github_api(
            f"https://api.github.com/repos/{repo_full_name}/pulls/{number}/comments?per_page=100",
            token,
        ) or []
        for rc in review_comments:
            if not isinstance(rc, dict):
                continue
            user_login = (rc.get("user") or {}).get("login")
            rbody = (rc.get("body") or "").strip()
            if user_login == owner and rbody:
                path = rc.get("path", "")
                line = rc.get("line") or rc.get("original_line") or ""
                ts = rc.get("updated_at") or rc.get("created_at") or ""
                timed_feedbacks.append((ts, f"[File: {path}:{line}] {rbody}"))
    except Exception:
        pass

    if not timed_feedbacks:
        return files_summary, "（追加コメントなし）"

    timed_feedbacks.sort(key=lambda x: x[0])
    latest_texts = [item[1] for item in timed_feedbacks[-3:]]
    return files_summary, "\n---\n".join(latest_texts)[:1500]


def get_common_rules() -> str:
    global_rules = load_global_rules()
    return f"""[MANDATORY TOOL EXECUTION & TOKEN-SAVING RULES]
You MUST actively use your terminal/bash and file editing tools to clone and inspect/modify the code.
1. Stack Detection & Rules Priority:
   Check root files ONLY (`ls -1`) inside `repo` to identify stack & test command immediately:
   - `package.json` -> Node/TS/JS (`npm test --silent` or `npx tsc --noEmit`)
   - `build.gradle*` -> Android/Kotlin/Java (`./gradlew test --quiet | tail -n 30`)
   - `pubspec.yaml` -> Flutter/Dart (`flutter test | tail -n 30`)
   - `pyproject.toml` / `requirements.txt` -> Python (`pytest -q`)
   - `Cargo.toml` -> Rust (`cargo check -q`)
   - `go.mod` -> Go (`go test ./...`)
   (Follow the Global Engineering Constitution below; if a repo-specific `AGENTS.md` exists, prioritize its repo-specific rules.)
2. Minimal Context Navigation:
   - NEVER `cat` entire large files or scan `.git`, `node_modules`, `build`, `.gradle`, `dist`, `.dart_tool`, or `vendor`.
   - Locate targets via `grep -rn "<keyword>" . --exclude-dir={{.git,node_modules,build,.gradle,dist,.dart_tool,vendor}}` and read ONLY surrounding lines with `sed -n 'X,Yp'`.
3. Root-Cause Fix & Quiet Verification:
   - Produce the smallest possible, high-value diff without reformatting unrelated code.
   - NEVER add external dependencies, upgrade lockfiles, or touch `.github/workflows/`.
   - NEVER print, log, or commit access tokens, credentials, or authenticated `git remote` URLs.
   - Always pipe build/test outputs through `| tail -n 30` to avoid log bloat.{global_rules}"""


def build_prompt(
    is_pr: bool,
    repo_full_name: str,
    base_branch: str,
    head_branch: str,
    number: int,
    title: str,
    body: str,
    files_summary: str,
    feedback: str,
    github_token: str,
) -> str:
    auth_repo_url = f"https://x-access-token:{github_token}@github.com/{repo_full_name}.git"
    common_rules = get_common_rules()

    if is_pr:
        return f"""Task: Use your bash/terminal tool to handle Pull Request #{number} in `{repo_full_name}` (branch: `{head_branch}`) based on the reviewer's feedback.

[ORIGINAL PR #{number}: {title}]
{body}

[FILES MODIFIED IN THIS PR]
{files_summary}

[LATEST REVIEW FEEDBACK TO ADDRESS]
{feedback}

{common_rules}

4. Execute Git Clone, Edit (if needed), Commit & Push:
   Run these shell commands:
     git clone --depth 1 -b "{head_branch}" "{auth_repo_url}" repo && cd repo
     git config user.name "Antigravity Bot"
     git config user.email "antigravity-bot@users.noreply.github.com"
   - If code changes are requested: modify the files, verify with tests, and push (`git add -A && git commit -m "fix: address review feedback on #{number}" && git push origin "{head_branch}"`).
   - If the feedback is a question or explicitly says not to modify code: do NOT commit or push anything; simply answer concisely in the summary below.

5. Final Output Format (STRICT):
   Output ONLY this 1 line in Japanese at the very end. No greetings, no logs:
   要約: <1-sentence summary of the action or answer within 80 chars>
"""
    else:
        return f"""Task: Use your bash/terminal tool to handle GitHub issue #{number} in `{repo_full_name}` (base: `{base_branch}`, target branch: `{head_branch}`).

[ISSUE #{number}: {title}]
{body}

{common_rules}

4. Execute Git Clone, Edit (if needed), Commit & Push:
   Run these shell commands:
     git clone --depth 1 -b "{base_branch}" "{auth_repo_url}" repo && cd repo
     git config user.name "Antigravity Bot"
     git config user.email "antigravity-bot@users.noreply.github.com"
   - If code changes are needed: implement the fix, verify with tests, and push (`git fetch origin "{head_branch}" --depth=1 2>/dev/null || true && git checkout -B "{head_branch}" && git add -A && git commit -m "fix: resolve #{number}" && git push -u origin "{head_branch}" --force-with-lease`).
   - If the issue is a dry-run test, investigation-only, or explicitly instructs NOT to modify code: do NOT commit or push any branch; simply report the result in the summary below.

5. Final Output Format (STRICT):
   Output ONLY this 1 line in Japanese at the very end. No greetings, no logs:
   要約: <1-sentence summary of the fix, investigation, or dry-run result within 80 chars>
"""


def build_auto_improve_prompt(
    repo_full_name: str,
    base_branch: str,
    head_branch: str,
    github_token: str,
) -> str:
    auth_repo_url = f"https://x-access-token:{github_token}@github.com/{repo_full_name}.git"
    common_rules = get_common_rules()

    return f"""Task: Autonomous Code Quality & Reliability Patrol for `{repo_full_name}` (base: `{base_branch}`, target branch: `{head_branch}`).
There are currently no pending user issues. Your mission is to inspect the codebase, find ONE high-impact improvement, implement it safely, and verify it passes all tests.

{common_rules}

4. Inspection Priorities (Pick the single highest-value improvement):
   - Priority A: Missing unit tests or edge-case tests for core business logic / calculations / parsers.
   - Priority B: Subtle bug, unhandled null/empty state, race condition, or division-by-zero risk in existing logic.
   - Priority C: Type safety improvement (removing `any`, tightening types) or dead code / duplicated logic cleanup.
   - Do NOT make trivial cosmetic-only changes (like adding comments or renaming variables alone).

5. Execute Git Clone, Improve, Test & Push:
   Run these shell commands:
     git clone --depth 1 -b "{base_branch}" "{auth_repo_url}" repo && cd repo
     git config user.name "Antigravity Bot"
     git config user.email "antigravity-bot@users.noreply.github.com"
   - Inspect key source files and existing tests, implement the single best improvement, and run the project's test/typecheck suite to confirm zero regressions.
   - If you made a solid, verified improvement:
     git fetch origin "{head_branch}" --depth=1 2>/dev/null || true
     git checkout -B "{head_branch}"
     git add -A && git commit -m "chore: autonomous code quality and test improvement"
     git push -u origin "{head_branch}" --force-with-lease
   - If the repository is empty, has no testable source files, or tests fail and cannot be cleanly resolved: do NOT push anything.

6. Final Output Format (STRICT):
   Output ONLY these 2 lines in Japanese at the very end. No greetings, no logs:
   タイトル: <Concise Japanese PR title within 45 chars>
   要約: <2-3 bullet points in Japanese explaining what was improved and why>
"""


def start_agent_async(gemini_key: str, prompt: str) -> str:
    payload = {
        "agent": "antigravity-preview-09-2026",
        "input": prompt,
        "background": True,
        "environment": {
            "type": "remote",
        },
    }
    res = gemini_api("interactions", gemini_key, method="POST", data=payload) or {}
    interaction_id = res.get("id") or res.get("name", "").split("/")[-1]
    if not interaction_id:
        raise RuntimeError(f"interaction_id 取得失敗: {res}")
    return interaction_id


def check_wip_items(owner: str, gemini_key: str, github_token: str) -> tuple[bool, int]:
    has_error = False
    wip_items = find_items_by_label(owner, RUNNING_LABEL, github_token)

    for item in wip_items:
        repo_api_url = item.get("repository_url", "")
        repo_full_name = repo_api_url.replace("https://api.github.com/repos/", "")
        number = item.get("number")
        if not repo_full_name or not number:
            continue

        is_pr = "pull_request" in item
        title = item.get("title") or ""
        mode_label = "PR対応" if is_pr else "Issue対応"

        try:
            comments = get_all_issue_comments(repo_full_name, number, github_token)
            meta_match = None
            for c in reversed(comments):
                m = re.search(r"<!-- antigravity:id=([^;]+);sha=([^;]*) -->", c.get("body") or "")
                if m:
                    meta_match = m
                    break

            if not meta_match:
                raise RuntimeError("追跡用タスクIDがコメントから見つかりませんでした。")

            interaction_id, before_sha = meta_match.group(1), meta_match.group(2)
            status_obj = gemini_api(f"interactions/{interaction_id}", gemini_key, method="GET") or {}
            status = status_obj.get("status")

            if status not in ("completed", "failed", "cancelled"):
                print(f"継続中 ({mode_label}): {repo_full_name} #{number} (status={status})")
                continue

            if status in ("failed", "cancelled"):
                err_detail = json.dumps(status_obj, ensure_ascii=False)[:400]
                raise RuntimeError(f"status={status} ({err_detail})")

            outputs = status_obj.get("outputs") or []
            texts = [out.get("text", "").strip() for out in outputs if isinstance(out, dict) and out.get("text")]
            raw_summary = "\n".join(texts[-1].splitlines()[-3:]) if texts else "要約: 確認・テスト実行を完了しました（コード変更なし）。"
            summary = sanitize(raw_summary, gemini_key, github_token)

            if is_pr:
                pr_info = github_api(f"https://api.github.com/repos/{repo_full_name}/pulls/{number}", github_token) or {}
                base_branch = (pr_info.get("base") or {}).get("ref") or "main"
                head_branch = (pr_info.get("head") or {}).get("ref") or ""
            else:
                repo_info = github_api(repo_api_url, github_token) or {}
                base_branch = repo_info.get("default_branch") or "main"
                head_branch = f"fix/issue-{number}"

            after_sha = get_branch_sha(repo_full_name, head_branch, github_token)
            has_new_push = bool(after_sha and before_sha != after_sha)

            if has_new_push and has_commits_ahead(repo_full_name, base_branch, head_branch, github_token):
                if not is_pr:
                    pr_url, pr_num = ensure_pr_exists(
                        repo_full_name, owner, base_branch, head_branch, number, title, summary, github_token
                    )
                    comment_body = f"✅ 自動{mode_label}完了\nPR: {pr_url}\n{summary}"
                    review_pull_request(repo_full_name, pr_num, gemini_key, github_token)
                else:
                    comment_body = f"✅ 自動{mode_label}完了 (`{after_sha[:7]}`)\n{summary}"
                    review_pull_request(repo_full_name, number, gemini_key, github_token)
            else:
                comment_body = f"✅ 自動{mode_label}完了（コード変更なし）\n{summary}"

            set_exclusive_label(
                repo_full_name,
                number,
                DONE_LABEL,
                github_token,
                current_labels=item.get("labels"),
            )
            post_comment(repo_full_name, number, comment_body, github_token)
            print(f"回収完了 ({mode_label}): {repo_full_name} #{number}")

        except Exception as e:
            has_error = True
            safe_err = sanitize(str(e), gemini_key, github_token)[:400]
            try:
                set_exclusive_label(
                    repo_full_name,
                    number,
                    FAILED_LABEL,
                    github_token,
                    current_labels=item.get("labels"),
                )
                post_comment(repo_full_name, number, f"⚠️ 自動{mode_label}中断: `{safe_err}`", github_token)
            except Exception:
                pass
            print(f"エラー: {repo_full_name} #{number} -> {safe_err}", file=sys.stderr)

    return has_error, len(wip_items)


def dispatch_new_items(owner: str, gemini_key: str, github_token: str) -> tuple[bool, int]:
    has_error = False
    items = find_items_by_label(owner, TARGET_LABEL, github_token)

    for item in items:
        repo_api_url = item.get("repository_url", "")
        repo_full_name = repo_api_url.replace("https://api.github.com/repos/", "")
        number = item.get("number")
        if not repo_full_name or not number:
            continue

        is_pr = "pull_request" in item
        title = item.get("title") or ""
        body = (item.get("body") or "詳細なし")[:1500]
        mode_label = "PR対応" if is_pr else "Issue対応"

        try:
            if is_pr:
                pr_info = github_api(f"https://api.github.com/repos/{repo_full_name}/pulls/{number}", github_token) or {}
                head_info = pr_info.get("head") or {}
                base_info = pr_info.get("base") or {}
                head_repo_owner = ((head_info.get("repo") or {}).get("owner") or {}).get("login")
                if head_repo_owner != owner:
                    continue
                base_branch = base_info.get("ref") or "main"
                head_branch = head_info.get("ref")
                if not head_branch:
                    raise RuntimeError("PRのブランチ名(head.ref)が取得できませんでした。")
                files_summary, feedback = get_pr_context(repo_full_name, number, owner, github_token)
            else:
                if (item.get("user") or {}).get("login") != owner:
                    continue
                repo_info = github_api(repo_api_url, github_token) or {}
                base_branch = repo_info.get("default_branch") or "main"
                head_branch = f"fix/issue-{number}"
                files_summary, feedback = "", ""

            before_sha = get_branch_sha(repo_full_name, head_branch, github_token)
            prompt = build_prompt(
                is_pr,
                repo_full_name,
                base_branch,
                head_branch,
                number,
                title,
                body,
                files_summary,
                feedback,
                github_token,
            )

            interaction_id = start_agent_async(gemini_key, prompt)
            set_exclusive_label(
                repo_full_name,
                number,
                RUNNING_LABEL,
                github_token,
                current_labels=item.get("labels"),
            )
            post_comment(
                repo_full_name,
                number,
                f"⏳ 自動{mode_label}を受け付けました（次回の巡回で完了を回収します）\n<!-- antigravity:id={interaction_id};sha={before_sha} -->",
                github_token,
            )
            print(f"投入完了 ({mode_label}): {repo_full_name} #{number} -> ID={interaction_id}")

        except Exception as e:
            has_error = True
            safe_err = sanitize(str(e), gemini_key, github_token)[:400]
            try:
                set_exclusive_label(
                    repo_full_name,
                    number,
                    FAILED_LABEL,
                    github_token,
                    current_labels=item.get("labels"),
                )
                post_comment(repo_full_name, number, f"⚠️ 自動{mode_label}中断: `{safe_err}`", github_token)
            except Exception:
                pass
            print(f"エラー: {repo_full_name} #{number} -> {safe_err}", file=sys.stderr)

    return has_error, len(items)


def select_auto_improve_repo(candidates: list[dict], now_jst: datetime) -> dict:
    """15分スロットごとに候補リポジトリをローテーションして1件選ぶ"""
    slot = now_jst.day * 24 * 4 + now_jst.hour * 4 + (now_jst.minute // 15)
    return candidates[slot % len(candidates)]


def find_tracker_issue(owner: str, token: str) -> int:
    """司令塔リポジトリ内の自律お掃除状態管理Issue番号を返す（無ければ0）"""
    self_repo = f"{owner}/{SELF_REPO_NAME}"
    issues = github_api(
        f"https://api.github.com/repos/{self_repo}/issues?state=open&per_page=30",
        token,
    ) or []
    for issue in issues:
        if isinstance(issue, dict) and "pull_request" not in issue and issue.get("title") == AUTO_IMPROVE_TRACKER_TITLE:
            return int(issue.get("number") or 0)
    return 0


def get_or_create_tracker_issue(owner: str, token: str) -> int:
    existing = find_tracker_issue(owner, token)
    if existing:
        return existing
    self_repo = f"{owner}/{SELF_REPO_NAME}"
    created = github_api(
        f"https://api.github.com/repos/{self_repo}/issues",
        token,
        method="POST",
        data={
            "title": AUTO_IMPROVE_TRACKER_TITLE,
            "body": "自律お掃除モード（Auto-Improve）が8分以内に完了しなかった場合の次回回収用トラッカーです。（自動管理）",
        },
    ) or {}
    return int(created.get("number") or 0)


def finalize_auto_improve(
    repo_full_name: str,
    owner: str,
    base_branch: str,
    target_branch: str,
    status_obj: dict,
    now_jst: datetime,
    gemini_key: str,
    github_token: str,
):
    """自律お掃除完了後のブランチ確認・Draft PR作成・AI査読を実行する"""
    after_sha = get_branch_sha(repo_full_name, target_branch, github_token)
    if not after_sha or not has_commits_ahead(repo_full_name, base_branch, target_branch, github_token):
        print(f"自律お掃除モード: {repo_full_name} は改善不要（変更なし）と判断されました。")
        return

    outputs = status_obj.get("outputs") or []
    texts = [out.get("text", "").strip() for out in outputs if isinstance(out, dict) and out.get("text")]
    raw_out = sanitize(texts[-1], gemini_key, github_token) if texts else ""

    pr_title = f"自律コード品質改善 ({now_jst.strftime('%Y-%m-%d')})"
    summary_lines = []
    for line in raw_out.splitlines():
        sline = line.strip()
        if sline.startswith("タイトル:"):
            pr_title = sline.replace("タイトル:", "").strip()[:60] or pr_title
        elif sline:
            summary_lines.append(sline)

    summary_text = "\n".join(summary_lines[-6:]) or "コード品質およびテストカバレッジの改善を実施しました。"
    pr_url, pr_num = ensure_pr_exists(
        repo_full_name,
        owner,
        base_branch,
        target_branch,
        0,
        pr_title,
        summary_text,
        github_token,
        draft=True,
    )
    print(f"✨ 自律お掃除Draft PR作成完了: {pr_url}")

    # 作成したDraft PRに対してAI査読官による自動差分レビューを実行
    review_pull_request(repo_full_name, pr_num, gemini_key, github_token)


def check_pending_auto_improve(owner: str, gemini_key: str, github_token: str) -> tuple[bool, bool]:
    """持ち越された自律お掃除タスクがあれば回収する。(has_error, still_running) を返す"""
    if not ENABLE_AUTO_IMPROVE:
        return False, False

    tracker_num = find_tracker_issue(owner, github_token)
    if not tracker_num:
        return False, False

    self_repo = f"{owner}/{SELF_REPO_NAME}"
    comments = get_all_issue_comments(self_repo, tracker_num, github_token)
    pending_meta = None
    for c in reversed(comments):
        body = c.get("body") or ""
        if "<!-- antigravity:auto-improve-done;" in body:
            return False, False
        m = re.search(
            r"<!-- antigravity:auto-improve-wip;id=([^;]+);repo=([^;]+);base=([^;]+);branch=([^;]+) -->",
            body,
        )
        if m:
            pending_meta = m
            break

    if not pending_meta:
        return False, False

    interaction_id, repo_full_name, base_branch, target_branch = (
        pending_meta.group(1),
        pending_meta.group(2),
        pending_meta.group(3),
        pending_meta.group(4),
    )

    try:
        status_obj = gemini_api(f"interactions/{interaction_id}", gemini_key, method="GET") or {}
        status = status_obj.get("status")
        if status not in ("completed", "failed", "cancelled"):
            print(f"自律お掃除持ち越し継続中: {repo_full_name} (ID={interaction_id}, status={status})")
            return False, True

        if status == "completed":
            finalize_auto_improve(
                repo_full_name,
                owner,
                base_branch,
                target_branch,
                status_obj,
                datetime.now(JST),
                gemini_key,
                github_token,
            )
        else:
            print(f"自律お掃除持ち越し終了 (status={status}): {repo_full_name}")

        post_comment(
            self_repo,
            tracker_num,
            f"✅ 自律お掃除回収完了 (`{repo_full_name}` / status={status})\n<!-- antigravity:auto-improve-done;id={interaction_id} -->",
            github_token,
        )
        return False, False

    except Exception as e:
        safe_err = sanitize(str(e), gemini_key, github_token)[:400]
        try:
            post_comment(
                self_repo,
                tracker_num,
                f"⚠️ 自律お掃除回収エラー (`{repo_full_name}`): `{safe_err}`\n<!-- antigravity:auto-improve-done;id={interaction_id} -->",
                github_token,
            )
        except Exception:
            pass
        print(f"自律お掃除持ち越し回収エラー ({repo_full_name}): {safe_err}", file=sys.stderr)
        return True, False


def run_autonomous_improvement(owner: str, gemini_key: str, github_token: str) -> bool:
    if not ENABLE_AUTO_IMPROVE:
        return False

    now_jst = datetime.now(JST)
    today_str = now_jst.strftime("%Y%m%d")
    target_branch = f"chore/auto-improve-{today_str}"

    repos = github_api(
        "https://api.github.com/user/repos?affiliation=owner&sort=pushed&direction=desc&per_page=30",
        github_token,
    ) or []

    candidates = []
    already_created_today = 0

    for r in repos:
        if not isinstance(r, dict):
            continue
        if r.get("fork") or r.get("archived") or r.get("disabled"):
            continue
        name = r.get("name") or ""
        full_name = r.get("full_name") or ""
        topics = r.get("topics") or []
        if not full_name or name == SELF_REPO_NAME or "auto-ignore" in topics:
            continue
        if (r.get("size") or 0) == 0:
            continue

        if get_branch_sha(full_name, target_branch, github_token):
            already_created_today += 1
            continue

        candidates.append(r)

    if already_created_today >= MAX_AUTO_IMPROVE_PER_DAY:
        print(f"自律お掃除モード: 本日の上限 ({MAX_AUTO_IMPROVE_PER_DAY}件) に到達済みのためスキップします。")
        return False

    if not candidates:
        print("自律お掃除モード: 対象候補リポジトリなし")
        return False

    target_repo = select_auto_improve_repo(candidates, now_jst)
    repo_full_name = target_repo["full_name"]
    base_branch = target_repo.get("default_branch") or "main"

    print(f"自律お掃除モード起動: {repo_full_name} (ブランチ: {target_branch})")

    try:
        prompt = build_auto_improve_prompt(repo_full_name, base_branch, target_branch, github_token)
        interaction_id = start_agent_async(gemini_key, prompt)
        print(f"自律お掃除タスク投入完了: {repo_full_name} -> ID={interaction_id}（最大8分間完了を待機してDraft PR化します）")

        for _ in range(48):
            time.sleep(10)
            status_obj = gemini_api(f"interactions/{interaction_id}", gemini_key, method="GET") or {}
            status = status_obj.get("status")
            if status in ("completed", "failed", "cancelled"):
                break
        else:
            tracker_num = get_or_create_tracker_issue(owner, github_token)
            if tracker_num:
                post_comment(
                    f"{owner}/{SELF_REPO_NAME}",
                    tracker_num,
                    f"⏳ 自律お掃除タスクを持ち越します: `{repo_full_name}` (ブランチ: `{target_branch}`)\n<!-- antigravity:auto-improve-wip;id={interaction_id};repo={repo_full_name};base={base_branch};branch={target_branch} -->",
                    github_token,
                )
            print(f"自律お掃除モード: {repo_full_name} が8分以内に完了しなかったためトラッカーIssue #{tracker_num} に記録して次回に持ち越します。")
            return False

        if status != "completed":
            print(f"自律お掃除モード終了 (status={status}): {repo_full_name}")
            return False

        finalize_auto_improve(
            repo_full_name,
            owner,
            base_branch,
            target_branch,
            status_obj,
            now_jst,
            gemini_key,
            github_token,
        )
        return False

    except Exception as e:
        safe_err = sanitize(str(e), gemini_key, github_token)[:400]
        print(f"自律お掃除モードエラー ({repo_full_name}): {safe_err}", file=sys.stderr)
        return True


def main():
    gemini_key = os.environ.get("GEMINI_API_KEY")
    github_token = os.environ.get("CROSS_REPO_TOKEN")
    if not gemini_key or not github_token:
        sys.exit("エラー: 環境変数が未設定です。")

    owner = get_authenticated_user(github_token)

    err1, wip_count = check_wip_items(owner, gemini_key, github_token)
    err_pending_ai, auto_improve_running = check_pending_auto_improve(owner, gemini_key, github_token)
    err2, new_count = dispatch_new_items(owner, gemini_key, github_token)

    err3 = False
    if wip_count == 0 and new_count == 0 and not auto_improve_running:
        err3 = run_autonomous_improvement(owner, gemini_key, github_token)

    if err1 or err_pending_ai or err2 or err3:
        sys.exit(1)


if __name__ == "__main__":
    main()