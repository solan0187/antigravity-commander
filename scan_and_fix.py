import os
import sys
import time
import urllib.request
import urllib.parse
import json
from google import genai

TARGET_LABEL = "auto-fix"
RUNNING_LABEL = "antigravity-wip"
DONE_LABEL = "antigravity-done"
MAX_ISSUES_PER_RUN = 3


def github_api(url: str, token: str, method: str = "GET", data: dict = None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    if body:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as res:
        if res.status == 204:
            return None
        return json.loads(res.read().decode("utf-8"))


def get_authenticated_user(token: str) -> str:
    user = github_api("https://api.github.com/user", token)
    return user["login"]


def find_target_issues(owner: str, token: str) -> list:
    query = urllib.parse.quote(f'user:{owner} is:issue is:open label:"{TARGET_LABEL}"')
    url = f"https://api.github.com/search/issues?q={query}&sort=created&order=asc&per_page={MAX_ISSUES_PER_RUN}"
    result = github_api(url, token)
    return result.get("items", [])


def swap_labels(repo_full_name: str, issue_num: int, remove_label: str, add_label: str, token: str):
    try:
        encoded_remove = urllib.parse.quote(remove_label)
        github_api(
            f"https://api.github.com/repos/{repo_full_name}/issues/{issue_num}/labels/{encoded_remove}",
            token,
            method="DELETE",
        )
    except Exception:
        pass

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


def run_antigravity_agent(client, repo_full_name: str, default_branch: str, issue_num: int, github_token: str):
    branch_name = f"fix/issue-{issue_num}-antigravity"

    prompt = f"""
あなたは熟練のソフトウェアエンジニアです。
クラウドのサンドボックス環境内で以下の手順を実行し、GitHubのIssueを修正してプルリクエストを作成してください。

【対象情報】
- リポジトリ: {repo_full_name}
- 対象Issue番号: #{issue_num}
- ベースブランチ: {default_branch}
- 作業ブランチ名: {branch_name}

【実行ステップ】
1. Git認証とリポジトリのクローン
   - まず以下の環境変数を設定する:
     `export GITHUB_TOKEN="{github_token}"`
     `export GH_TOKEN="{github_token}"`
   - トークンをURLに出さずに認証設定を行う:
     `git config --global url."https://x-access-token:${{GITHUB_TOKEN}}@github.com/".insteadOf "https://github.com/"`
   - `git config --global user.name "Antigravity Bot"`
   - `git config --global user.email "antigravity-bot@users.noreply.github.com"`
   - `git clone https://github.com/{repo_full_name}.git` を実行してディレクトリへ移動する。
   - `gh issue view {issue_num}`（使えない場合はGitHub REST API）でIssueのタイトル・本文・コメントを読み込む。

2. コード調査と修正実装
   - Issueの要件やバグを把握し、関連コードを調査して修正する。
   - `.github/workflows/` 配下のファイルは変更しないこと。

3. ビルド・テストの検証
   - リポジトリ内のテストやビルドコマンドを探して実行し、問題がないことを確認する。

4. コミットとPR作成
   - `{branch_name}` ブランチを作成（既存なら `-B`）してコミット・pushする。
   - `gh pr create`（またはGitHub REST API）で `{default_branch}` 宛てのPRを作成する。
   - PR本文には「Closes #{issue_num}」「原因の解説」「変更内容の要約」「テスト結果」を日本語で記載する。

5. 最終報告
   - 作成したPRのURLと修正のポイントを簡潔に報告する。
"""

    interaction = client.interactions.create(
        agent="antigravity-preview-09-2026",
        input=prompt,
        background=True,
        agent_config={
            "type": "antigravity",
        },
    )

    interaction_id = interaction.id
    print(f"[{repo_full_name} #{issue_num}] エージェント起動 (タスクID: {interaction_id})")

    while True:
        status_obj = client.interactions.get(interaction_id)
        status = getattr(status_obj, "status", None)

        if status == "completed":
            output_texts = []
            if hasattr(status_obj, "outputs") and status_obj.outputs:
                for out in status_obj.outputs:
                    if hasattr(out, "text") and out.text:
                        output_texts.append(out.text)
            return "\n".join(output_texts)

        elif status in ("failed", "cancelled"):
            raise RuntimeError(f"エージェント実行失敗 (status: {status}): {status_obj}")

        time.sleep(10)


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
    client = genai.Client(api_key=gemini_key, http_options={"api_version": "v1beta"})

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
            summary = run_antigravity_agent(client, repo_full_name, default_branch, issue_num, github_token)
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
            post_comment(
                repo_full_name,
                issue_num,
                f"⚠️ 自動修正中にエラーが発生したため中断しました。\n```\n{e}\n```",
                github_token,
            )


if __name__ == "__main__":
    main()