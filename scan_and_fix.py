import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import json

TARGET_LABEL = "auto-fix"
RUNNING_LABEL = "antigravity-wip"
DONE_LABEL = "antigravity-done"
FAILED_LABEL = "antigravity-failed"
MAX_ITEMS_PER_RUN = 3
POLL_INTERVAL_SEC = 10
MAX_POLL_COUNT = 90  # 最大15分待機


def sanitize(text: str, *secrets: str) -> str:
    """ログやIssueコメントにトークンが絶対に出力されないようマスクする"""
    res = str(text)
    for s in secrets:
        if s and len(s) > 4:
            res = res.replace(s, "***")
    return res


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
                wait = 3 * (attempt + 1)
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code} ({url}): {err_body}") from e
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                wait = 3 * (attempt + 1)
                time.sleep(wait)
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


def find_target_items(owner: str, token: str) -> list:
    items = []
    # GitHub Search APIの必須仕様に合わせ、is:issue と is:pull-request を分けて検索する
    for item_type in ("is:issue", "is:pull-request"):
        query = urllib.parse.quote(f'user:{owner} {item_type} is:open label:"{TARGET_LABEL}"')
        url = f"https://api.github.com/search/issues?q={query}&sort=updated&order=asc&per_page={MAX_ITEMS_PER_RUN}"
        res = github_api(url, token) or {}
        items.extend(res.get("items", []))
    # 更新日時が古い順に並べ替えて上限件数まで返す
    items.sort(key=lambda x: x.get("updated_at") or "")
    return items[:MAX_ITEMS_PER_RUN]


def set_exclusive_label(repo_full_name: str, number: int, add_label: str, token: str):
    for old_label in (TARGET_LABEL, RUNNING_LABEL, DONE_LABEL, FAILED_LABEL):
        if old_label == add_label:
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

    if add_label:
        github_api(
            f"https://api.github.com/repos/{repo_full_name}/issues/{number}/labels",
            token,
            method="POST",
            data={"labels": [add_label]},
        )


def post_comment(repo_full_name: str, number: int, body: str, token: str):
    github_api(
        f"https://api.github.com/repos/{repo_full_name}/issues/{number}/comments",
        token,
        method="POST",
        data={"body": body},
    )


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


def ensure_pr_exists(
    repo_full_name: str,
    owner: str,
    base_branch: str,
    head_branch: str,
    issue_num: int,
    issue_title: str,
    summary: str,
    token: str,
) -> str:
    """PRが存在するか確認し、AIが作り忘れていた場合はPython側で自動作成してURLを返す"""
    enc_head = urllib.parse.quote(f"{owner}:{head_branch}")
    prs = github_api(
        f"https://api.github.com/repos/{repo_full_name}/pulls?head={enc_head}&state=open",
        token,
    ) or []
    if prs and isinstance(prs, list) and prs[0].get("html_url"):
        return prs[0]["html_url"]

    # AIがブランチだけpushしてPR作成を忘れた場合のフォールバック作成
    pr_body = f"Closes #{issue_num}\n\n### 自動修正内容\n{summary}"
    created = github_api(
        f"https://api.github.com/repos/{repo_full_name}/pulls",
        token,
        method="POST",
        data={
            "title": f"fix: {issue_title} (#{issue_num})",
            "head": head_branch,
            "base": base_branch,
            "body": pr_body,
        },
    ) or {}
    return created.get("html_url") or ""


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

    comments = github_api(
        f"https://api.github.com/repos/{repo_full_name}/issues/{number}/comments?per_page=100",
        token,
    ) or []
    for c in comments:
        if not isinstance(c, dict):
            continue
        body = (c.get("body") or "").strip()
        user_login = (c.get("user") or {}).get("login")
        if (
            user_login == owner
            and body
            and not body.startswith("✅ 自動")
            and not body.startswith("⚠️ 自動")
            and not body.startswith("🤖 ")
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
        return files_summary, "（追加コメントなし。PR本文と変更ファイルを確認して修正してください）"

    timed_feedbacks.sort(key=lambda x: x[0])
    latest_texts = [item[1] for item in timed_feedbacks[-3:]]
    feedback_text = "\n---\n".join(latest_texts)[:1500]

    return files_summary, feedback_text


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
    common_rules = f"""[UNIVERSAL EXECUTION & TOKEN-SAVING RULES - STRICT]
1. Setup & Fast Stack Detection:
   export GITHUB_TOKEN="{github_token}" GH_TOKEN="{github_token}"
   git config --global url."https://x-access-token:${{GITHUB_TOKEN}}@github.com/".insteadOf "https://github.com/"
   git config --global user.name "Antigravity Bot"
   git config --global user.email "antigravity-bot@users.noreply.github.com"
   Check root files ONLY (`ls -1`) after cloning to identify stack & test command immediately:
   - `package.json` -> Node/TS/JS (`npm test --silent` or `npx tsc --noEmit`)
   - `build.gradle*` -> Android/Kotlin/Java (`./gradlew test --quiet | tail -n 30`)
   - `pubspec.yaml` -> Flutter/Dart (`flutter test | tail -n 30`)
   - `pyproject.toml` / `requirements.txt` -> Python (`pytest -q`)
   - `Cargo.toml` -> Rust (`cargo check -q`)
   - `go.mod` -> Go (`go test ./...`)
   (If a repo-specific `AGENTS.md` exists, prioritize its instructions.)

2. Minimal Context Navigation:
   - NEVER `cat` entire large files or scan `.git`, `node_modules`, `build`, `.gradle`, `dist`, `.dart_tool`, or `vendor`.
   - Locate targets via `grep -rn "<keyword>" . --exclude-dir={{.git,node_modules,build,.gradle,dist,.dart_tool,vendor}}` and read ONLY surrounding lines with `sed -n 'X,Yp'`.

3. Root-Cause Fix & Quiet Verification:
   - Produce the smallest possible diff without reformatting unrelated code.
   - NEVER add external dependencies, upgrade lockfiles, or touch `.github/workflows/`.
   - Always pipe build/test outputs through `| tail -n 30` to avoid log bloat.
   - NEVER add, remove, or modify labels or comments on the Issue or PR (handled externally)."""

    if is_pr:
        return f"""Update existing Pull Request #{number} in `{repo_full_name}` (branch: `{head_branch}`) based on the reviewer's feedback.

[ORIGINAL PR #{number}: {title}]
{body}

[FILES MODIFIED IN THIS PR]
{files_summary}

[LATEST REVIEW FEEDBACK TO ADDRESS]
{feedback}

{common_rules}

4. Clone PR Branch, Apply Fix & Push Update:
   git clone --depth 1 -b "{head_branch}" "https://github.com/{repo_full_name}.git" repo && cd repo
   Inspect the files listed under [FILES MODIFIED IN THIS PR], apply the requested changes, and verify with tests.
   Commit and push directly to `{head_branch}`:
   git add -A && git commit -m "fix: address review feedback on #{number}"
   git push origin "{head_branch}"

5. Final Output Format (STRICT):
   Output ONLY this 1 line in Japanese at the very end. No greetings, no logs:
   要約: <1-sentence summary of the additional fix within 60 chars>
"""
    else:
        return f"""Fix GitHub issue #{number} in `{repo_full_name}` (base: `{base_branch}`, head: `{head_branch}`).

[ISSUE #{number}: {title}]
{body}

{common_rules}

4. Clone, Commit & Push Branch:
   git clone --depth 1 -b "{base_branch}" "https://github.com/{repo_full_name}.git" repo && cd repo
   Implement the fix and verify with tests.
   git checkout -B "{head_branch}"
   git add -A && git commit -m "fix: resolve #{number}"
   git push -u origin "{head_branch}" --force
   (Note: Pushing the branch is mandatory. You may also create a PR to `{base_branch}` with `Closes #{number}` in Japanese, or the external controller will create it automatically.)

5. Final Output Format (STRICT):
   Output ONLY this 1 line in Japanese at the very end. No greetings, no logs:
   要約: <1-sentence summary of the fix within 60 chars>
"""


def execute_agent(gemini_key: str, repo_full_name: str, number: int, prompt: str) -> str:
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

    print(f"[{repo_full_name} #{number}] エージェント実行中 (ID: {interaction_id})")

    for _ in range(MAX_POLL_COUNT):
        time.sleep(POLL_INTERVAL_SEC)
        status_obj = gemini_api(f"interactions/{interaction_id}", gemini_key, method="GET") or {}
        status = status_obj.get("status")

        if status == "completed":
            outputs = status_obj.get("outputs") or []
            texts = [out.get("text", "").strip() for out in outputs if isinstance(out, dict) and out.get("text")]
            if not texts:
                return "要約: コードの修正を適用しました。"
            final_lines = [line for line in texts[-1].splitlines() if line.strip()]
            return "\n".join(final_lines[-3:])

        elif status in ("failed", "cancelled"):
            raise RuntimeError(f"status={status}")

    raise TimeoutError("15分タイムアウト")


def main():
    gemini_key = os.environ.get("GEMINI_API_KEY")
    github_token = os.environ.get("CROSS_REPO_TOKEN")
    if not gemini_key or not github_token:
        sys.exit("エラー: 環境変数が未設定です。")

    owner = get_authenticated_user(github_token)
    items = find_target_items(owner, github_token)
    if not items:
        print("対象アイテムなし")
        return

    has_error = False

    for item in items:
        repo_api_url = item.get("repository_url", "")
        repo_full_name = repo_api_url.replace("https://api.github.com/repos/", "")
        number = item.get("number")
        if not repo_full_name or not number:
            continue

        is_pr = "pull_request" in item
        title = item.get("title") or ""
        body = (item.get("body") or "詳細なし")[:1500]
        mode_label = "PR手直し" if is_pr else "Issue修正"

        try:
            if is_pr:
                pr_info = github_api(f"https://api.github.com/repos/{repo_full_name}/pulls/{number}", github_token) or {}
                head_info = pr_info.get("head") or {}
                base_info = pr_info.get("base") or {}
                head_repo_owner = ((head_info.get("repo") or {}).get("owner") or {}).get("login")

                if head_repo_owner != owner:
                    print(f"[{repo_full_name} #{number}] 外部フォークまたは削除済みリポジトリのPRのためスキップします。")
                    continue

                base_branch = base_info.get("ref") or "main"
                head_branch = head_info.get("ref")
                if not head_branch:
                    raise RuntimeError("PRのブランチ名(head.ref)が取得できませんでした。")

                files_summary, feedback = get_pr_context(repo_full_name, number, owner, github_token)
            else:
                if (item.get("user") or {}).get("login") != owner:
                    print(f"[{repo_full_name} #{number}] 外部ユーザーのIssueのためスキップします。")
                    continue

                repo_info = github_api(repo_api_url, github_token) or {}
                base_branch = repo_info.get("default_branch") or "main"
                head_branch = f"fix/issue-{number}"
                files_summary, feedback = "", ""

            # 実行前のブランチSHAを記録（Issueモード・PRモード共通）
            before_sha = get_branch_sha(repo_full_name, head_branch, github_token)

            print(f"開始 ({mode_label}): {repo_full_name} #{number}")
            set_exclusive_label(repo_full_name, number, RUNNING_LABEL, github_token)

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
            raw_summary = execute_agent(gemini_key, repo_full_name, number, prompt)
            summary = sanitize(raw_summary, gemini_key, github_token)

            # 裏取り検証1：実行後にブランチが存在し、かつSHAが確実に変化しているか
            after_sha = get_branch_sha(repo_full_name, head_branch, github_token)
            if not after_sha or before_sha == after_sha:
                raise RuntimeError(f"ブランチ '{head_branch}' に新しいコミットがpushされませんでした。({summary})")

            # 裏取り検証2：Issueモードの場合、PRが確実に存在するか確認し、無ければPython側で自動作成する
            if not is_pr:
                pr_url = ensure_pr_exists(
                    repo_full_name, owner, base_branch, head_branch, number, title, summary, github_token
                )
                comment_body = f"✅ 自動{mode_label}完了\nPR: {pr_url}\n{summary}"
            else:
                comment_body = f"✅ 自動{mode_label}完了 (`{after_sha[:7]}`)\n{summary}"

            set_exclusive_label(repo_full_name, number, DONE_LABEL, github_token)
            post_comment(repo_full_name, number, comment_body, github_token)
            print(f"完了 ({mode_label}): {repo_full_name} #{number}")

        except Exception as e:
            has_error = True
            safe_err = sanitize(str(e), gemini_key, github_token)[:300]
            try:
                set_exclusive_label(repo_full_name, number, FAILED_LABEL, github_token)
                post_comment(repo_full_name, number, f"⚠️ 自動{mode_label}中断: `{safe_err}`", github_token)
            except Exception:
                pass
            print(f"エラー: {repo_full_name} #{number} -> {safe_err}", file=sys.stderr)

    if has_error:
        sys.exit(1)


if __name__ == "__main__":
    main()