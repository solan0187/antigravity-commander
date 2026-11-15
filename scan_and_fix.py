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
    headers = {
        "x-goog-api-key": api_key,
    }
    return http_json(url, headers, method=method, data=data)


def get_authenticated_user(token: str) -> str:
    user = github_api("https://api.github.com/user", token)
    return user["login"]


def find_target_issues(owner: str, token: str) -> list:
    query = urllib.parse.quote(f'user:{owner} is:issue is:open label:"{TARGET_LABEL}"')
    url = f"https://api.github.com/search/issues?q={query}&sort=created&order=asc&per_page={MAX_ISSUES_PER_RUN}"
    result = github_api(url, token)
    return result.get("items", [])


def swap_labels(repo_full_name: str, issue_num: int, remove_label: str, add_label: str, token: str):
    if remove_label:
        try:
            encoded_remove = urllib.parse.quote(remove_label)
            github_api(
                f"https://api.github.com/repos/{repo_full_name}/issues/{issue_num}/labels/{encoded_remove}",
                token,
                method="DELETE",
            )
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


def run_antigravity_agent(gemini_key: str, repo_full_name: str, default_branch: str, issue_num: int, github_token: str) -> str:
    branch_name = f"fix/issue-{issue_num}-antigravity"

    prompt = f"""
あなたは熟練のソフトウェアエンジニアです。
サンドボックス環境内で以下の手順を実行し、GitHubのIssueを修正してプルリクエストを作成してください。

【対象情報】
- リポジトリ: {repo_full_name}
- 対象Issue番号: #{issue_num}
- ベースブランチ: {default_branch}
- 作業ブランチ名: {branch_name}

【実行ステップ】
1. Git認証とリポジトリのクローン
   - 以下の環境変数を設定する:
     `export GITHUB_TOKEN="{github_token}"`
     `export GH_TOKEN="{github_token}"`
   - トークンをURLに出さずに認証設定を行う:
     `git config --global url."https://x-access-token:${{GITHUB_TOKEN}}@github.com/".insteadOf "https://github.com/"`
   - `git config --global user.name "Antigravity Bot"`
   - `git config --global user.email "antigravity-bot@users.noreply.github.com"`
   - `git clone https://github.com/{repo_full_name}.git` を実行してディレクトリへ移動する。
   - `gh issue view {issue_num}` を実行してIssueのタイトル・本文・コメントを読み込む。
     （※ `gh` コマンドが未インストールの場合は、Pythonの `urllib.request` で `https://api.github.com/repos/{repo_full_name}/issues/{issue_num}` と `/comments` に `Authorization: Bearer $GITHUB_TOKEN` ヘッダを付けてGETリクエストし取得すること）

2. コード調査と修正実装
   - Issueの要件やバグを把握し、関連コードを調査して修正する。
   - セキュリティ上、`.github/workflows/` 配下のファイルは絶対に変更しないこと。

3. ビルド・テストの検証
   - リポジトリ内のテストやビルドコマンドを探して実行し、問題がないことを確認する。

4. コミットとPR作成
   - `git checkout -B {branch_name}` で作業ブランチを作成し、変更をコミットして `git push -u origin {branch_name}` でpushする。
   - `gh pr create --base {default_branch} --head {branch_name} --title "Fix #{issue_num}" --body "..."` でPRを作成する。
     （※ `gh` コマンドが使えない場合は、Pythonの `urllib.request` で `https://api.github.com/repos/{repo_full_name}/pulls` に `title`, `head`, `base`, `body` をJSONでPOSTして作成すること）
   - PR本文には「Closes #{issue_num}」「原因の解説」「変更内容の要約」「テスト結果」を日本語で記載する。

5. 最終報告
   - 作成したPRのURLと修正のポイントを簡潔に報告する。
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
        raise RuntimeError(f"interaction_id の取得に失敗しました: {res}")

    print(f"[{repo_full_name} #{issue_num}] エージェント起動 (タスクID: {interaction_id})")

    for _ in range(MAX_POLL_COUNT):
        time.sleep(POLL_INTERVAL_SEC)
        status_obj = gemini_api(f"interactions/{interaction_id}", gemini_key, method="GET")
        status = status_obj.get("status")

        if status == "completed":
            outputs = status_obj.get("outputs", [])
            texts = [out.get("text", "") for out in outputs if isinstance(out, dict) and out.get("text")]
            return "\n".join(texts) if texts else json.dumps(status_obj, ensure_ascii=False)

        elif status in ("failed", "cancelled"):
            raise RuntimeError(f"エージェント実行失敗 (status: {status}): {json.dumps(status_obj, ensure_ascii=False)}")

    raise TimeoutError(f"エージェントの処理がタイムアウトしました（{MAX_POLL_COUNT * POLL_INTERVAL_SEC}秒超過）")


def main():
    gemini_key = os.environ.get("GEMINI_API_KEY")
    github_token = os.environ.get("CROSS_REPO_TOKEN")

    if not gemini_key or not github_token:
        print("エラー: GEMINI_API_KEY と CROSS_REPO_TOKEN を設定してください。", file=sys.stderr)
        sys.exit(1)

    owner = get_authenticated_user(github_token)
    issues = find_target_issues(owner, github_token)

    if not issues:
        print(f"対象ラベル '{TARGET_LABEL}' が付いたIssueはありませんでした。")
        return

    print(f"{len(issues)} 件の対象Issueを検出しました。")

    for item in issues:
        repo_api_url = item["repository_url"]
        repo_full_name = repo_api_url.replace("https://api.github.com/repos/", "")
        issue_num = item["number"]

        if item["user"]["login"] != owner:
            print(f"[{repo_full_name} #{issue_num}] 作成者がオーナー以外のためスキップします。")
            continue

        repo_info = github_api(repo_api_url, github_token)
        default_branch = repo_info.get("default_branch", "main")

        print(f"\n--- 処理開始: {repo_full_name} #{issue_num} ({item['title']}) ---")
        swap_labels(repo_full_name, issue_num, TARGET_LABEL, RUNNING_LABEL, github_token)
        post_comment(
            repo_full_name,
            issue_num,
            "🤖 Antigravity Agent が修正作業を開始しました。完了次第PRを作成します。",
            github_token,
        )

        try:
            summary = run_antigravity_agent(gemini_key, repo_full_name, default_branch, issue_num, github_token)
            swap_labels(repo_full_name, issue_num, RUNNING_LABEL, DONE_LABEL, github_token)
            post_comment(
                repo_full_name,
                issue_num,
                f"✅ 修正作業が完了しました。\n\n{summary}",
                github_token,
            )
            print(f"--- 処理完了: {repo_full_name} #{issue_num} ---")

        except Exception as e:
            print(f"エラー発生 ({repo_full_name} #{issue_num}): {e}", file=sys.stderr)
            swap_labels(repo_full_name, issue_num, RUNNING_LABEL, FAILED_LABEL, github_token)
            post_comment(
                repo_full_name,
                issue_num,
                f"⚠️ 自動修正中にエラーが発生したため中断しました。\n```\n{e}\n```",
                github_token,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()