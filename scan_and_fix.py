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
MAX_ISSUES_PER_RUN = 3
POLL_INTERVAL_SEC = 10
MAX_POLL_COUNT = 90  # 最大15分待機


def http_json(url: str, headers: dict, method: str = "GET", data: dict = None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req_headers = dict(headers)
    if body is not None:
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            if res.status == 204:
                return None
            raw = res.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} ({url}): {err_body}") from e


def github_api(url: str, token: str, method: str = "GET", data: dict = None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "antigravity-commander",
    }
    return http_json(url, headers, method=method, data=data)


def gemini_api(path: str, api_key: str, method: str = "GET", data: dict = None):
    url = f"https://generativelanguage.googleapis.com/v1beta/{path}"
    return http_json(url, {"x-goog-api-key": api_key}, method=method, data=data)


def get_authenticated_user(token: str) -> str:
    return github_api("https://api.github.com/user", token)["login"]


def find_target_issues(owner: str, token: str) -> list:
    query = urllib.parse.quote(f'user:{owner} is:issue is:open label:"{TARGET_LABEL}"')
    url = f"https://api.github.com/search/issues?q={query}&sort=created&order=asc&per_page={MAX_ISSUES_PER_RUN}"
    return github_api(url, token).get("items", [])


def swap_labels(repo_full_name: str, issue_num: int, remove_label: str, add_label: str, token: str):
    if remove_label:
        try:
            enc = urllib.parse.quote(remove_label)
            github_api(f"https://api.github.com/repos/{repo_full_name}/issues/{issue_num}/labels/{enc}", token, method="DELETE")
        except Exception:
            pass
    if add_label:
        github_api(
            f"https://api.github.com/repos/{repo_full_name}/issues/{issue_num}/labels",
            token,
            method="POST",
            data={"labels": [add_label]},
        )


def post_comment(repo_full_name: str, issue_num: int, body: str, token: str):
    github_api(
        f"https://api.github.com/repos/{repo_full_name}/issues/{issue_num}/comments",
        token,
        method="POST",
        data={"body": body},
    )


def run_antigravity_agent(
    gemini_key: str,
    repo_full_name: str,
    default_branch: str,
    issue_num: int,
    issue_title: str,
    issue_body: str,
    github_token: str,
) -> str:
    branch_name = f"fix/issue-{issue_num}"

    # 汎用AGENTS.mdのルール（スタック自動判別・探索制限・最小差分・出力圧縮）をすべて統合したプロンプト
    prompt = f"""Fix GitHub issue #{issue_num} in `{repo_full_name}` (base: `{default_branch}`, head: `{branch_name}`).

[ISSUE #{issue_num}: {issue_title}]
{issue_body}

[UNIVERSAL EXECUTION & TOKEN-SAVING RULES - STRICT]
1. Clone & Fast Stack Detection:
   export GITHUB_TOKEN="{github_token}" GH_TOKEN="{github_token}"
   git config --global url."https://x-access-token:${{GITHUB_TOKEN}}@github.com/".insteadOf "https://github.com/"
   git config --global user.name "Antigravity Bot"
   git config --global user.email "antigravity-bot@users.noreply.github.com"
   git clone --depth 1 -b {default_branch} https://github.com/{repo_full_name}.git repo && cd repo
   Check root files ONLY (`ls -1`) to identify stack & test command immediately:
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
   - Identify the exact cause before editing. Produce the smallest possible diff without reformatting unrelated code.
   - NEVER add external dependencies, upgrade lockfiles, or touch `.github/workflows/`.
   - Preserve existing public APIs, signatures, and coding style.
   - Always pipe build/test outputs through `| tail -n 30` to avoid log bloat.

4. Commit & PR (in Japanese):
   git checkout -B {branch_name}
   git add -A && git commit -m "fix: resolve #{issue_num}"
   git push -u origin {branch_name} --force
   Create a PR to `{default_branch}` via `gh pr create` or GitHub REST API (`POST /repos/{repo_full_name}/pulls`).
   PR title & body MUST be in concise Japanese:
   - Closes #{issue_num}
   - 原因と修正内容（箇条書き3行以内）

5. Final Output Format (STRICT):
   Output ONLY these 2 lines in Japanese at the very end. No greetings, no logs:
   PR: <URL of the created PR>
   要約: <1-sentence summary of the fix within 60 chars>
"""

    payload = {
        "agent": "antigravity-preview-09-2026",
        "input": prompt,
        "background": True,
        "environment": {
            "type": "remote",
        },
    }

    res = gemini_api("interactions", gemini_key, method="POST", data=payload)
    interaction_id = res.get("id") or res.get("name", "").split("/")[-1]
    if not interaction_id:
        raise RuntimeError(f"interaction_id 取得失敗: {res}")

    print(f"[{repo_full_name} #{issue_num}] 実行中 (ID: {interaction_id})")

    for _ in range(MAX_POLL_COUNT):
        time.sleep(POLL_INTERVAL_SEC)
        status_obj = gemini_api(f"interactions/{interaction_id}", gemini_key, method="GET")
        status = status_obj.get("status")

        if status == "completed":
            outputs = status_obj.get("outputs", [])
            texts = [out.get("text", "").strip() for out in outputs if isinstance(out, dict) and out.get("text")]
            if not texts:
                return "PRを作成しました（Pull requestsタブを確認してください）。"
            final_lines = [line for line in texts[-1].splitlines() if line.strip()]
            return "\n".join(final_lines[-5:])

        elif status in ("failed", "cancelled"):
            raise RuntimeError(f"status={status}")

    raise TimeoutError("15分タイムアウト")


def main():
    gemini_key = os.environ.get("GEMINI_API_KEY")
    github_token = os.environ.get("CROSS_REPO_TOKEN")
    if not gemini_key or not github_token:
        sys.exit("エラー: 環境変数が未設定です。")

    owner = get_authenticated_user(github_token)
    issues = find_target_issues(owner, github_token)
    if not issues:
        print("対象Issueなし")
        return

    for item in issues:
        repo_api_url = item["repository_url"]
        repo_full_name = repo_api_url.replace("https://api.github.com/repos/", "")
        issue_num = item["number"]

        if item["user"]["login"] != owner:
            continue

        repo_info = github_api(repo_api_url, github_token)
        default_branch = repo_info.get("default_branch", "main")
        issue_title = item.get("title", "")
        issue_body = (item.get("body") or "詳細なし")[:2000]

        print(f"開始: {repo_full_name} #{issue_num}")
        swap_labels(repo_full_name, issue_num, TARGET_LABEL, RUNNING_LABEL, github_token)

        try:
            summary = run_antigravity_agent(
                gemini_key, repo_full_name, default_branch, issue_num, issue_title, issue_body, github_token
            )
            swap_labels(repo_full_name, issue_num, RUNNING_LABEL, DONE_LABEL, github_token)
            post_comment(repo_full_name, issue_num, f"✅ 自動修正完了\n{summary}", github_token)
            print(f"完了: {repo_full_name} #{issue_num}")

        except Exception as e:
            swap_labels(repo_full_name, issue_num, RUNNING_LABEL, FAILED_LABEL, github_token)
            err_short = str(e)[:300]
            post_comment(repo_full_name, issue_num, f"⚠️ 自動修正中断: `{err_short}`", github_token)
            sys.exit(1)


if __name__ == "__main__":
    main()