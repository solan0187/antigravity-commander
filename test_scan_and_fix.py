import unittest
from datetime import datetime
from unittest.mock import patch

import scan_and_fix


class TestScanAndFix(unittest.TestCase):
    def test_sanitize_masks_long_secrets_only(self):
        text = "token=secret_12345 short=abcd"
        res = scan_and_fix.sanitize(text, "secret_12345", "abcd", "")
        self.assertEqual(res, "token=*** short=abcd")

    @patch("scan_and_fix.github_api")
    def test_post_comment_appends_bot_marker_once(self, mock_gh):
        scan_and_fix.post_comment("owner/repo", 1, "hello", "tok")
        _, kwargs = mock_gh.call_args
        body = kwargs["data"]["body"]
        self.assertTrue(body.endswith(scan_and_fix.BOT_COMMENT_MARKER))

        mock_gh.reset_mock()
        already = f"hello\n{scan_and_fix.BOT_COMMENT_MARKER}"
        scan_and_fix.post_comment("owner/repo", 1, already, "tok")
        _, kwargs2 = mock_gh.call_args
        self.assertEqual(kwargs2["data"]["body"].count(scan_and_fix.BOT_COMMENT_MARKER), 1)

    @patch("scan_and_fix.github_api")
    def test_get_all_issue_comments_paginates(self, mock_gh):
        page1 = [{"id": i} for i in range(100)]
        page2 = [{"id": 100}, {"id": 101}]
        mock_gh.side_effect = [page1, page2]

        comments = scan_and_fix.get_all_issue_comments("owner/repo", 42, "tok")
        self.assertEqual(len(comments), 102)
        self.assertEqual(mock_gh.call_count, 2)

    @patch("scan_and_fix.github_api")
    def test_get_pr_context_ignores_bot_marker_even_with_prefix(self, mock_gh):
        mock_gh.side_effect = [
            [{"filename": "app.py", "additions": 3, "deletions": 1}],
            [
                {
                    "user": {"login": "owner"},
                    "body": f"前置きです\n### 🔍 AI査読官レポート\n{scan_and_fix.BOT_COMMENT_MARKER}",
                    "created_at": "2026-11-18T10:00:00Z",
                },
                {
                    "user": {"login": "owner"},
                    "body": "ここを直してください",
                    "created_at": "2026-11-18T10:05:00Z",
                },
            ],
            [
                {
                    "user": {"login": "owner"},
                    "body": "変数名を見直して",
                    "path": "app.py",
                    "line": 12,
                    "created_at": "2026-11-18T10:06:00Z",
                }
            ],
        ]

        files_summary, feedback = scan_and_fix.get_pr_context("owner/repo", 7, "owner", "tok")
        self.assertIn("`app.py` (+3 / -1)", files_summary)
        self.assertNotIn("AI査読官レポート", feedback)
        self.assertIn("ここを直してください", feedback)
        self.assertIn("[File: app.py:12] 変数名を見直して", feedback)

    @patch("scan_and_fix.github_api")
    def test_set_exclusive_label_skips_unnecessary_calls(self, mock_gh):
        current = [{"name": scan_and_fix.TARGET_LABEL}, {"name": "bug"}]
        scan_and_fix.set_exclusive_label(
            "owner/repo",
            10,
            scan_and_fix.RUNNING_LABEL,
            "tok",
            current_labels=current,
        )
        # Only TARGET_LABEL should be deleted, and RUNNING_LABEL should be added (2 calls total)
        self.assertEqual(mock_gh.call_count, 2)
        first_call = mock_gh.call_args_list[0]
        self.assertEqual(first_call.kwargs["method"], "DELETE")
        self.assertIn(scan_and_fix.TARGET_LABEL, first_call.args[0])
        second_call = mock_gh.call_args_list[1]
        self.assertEqual(second_call.kwargs["method"], "POST")

    @patch("scan_and_fix.post_comment")
    @patch("scan_and_fix.set_exclusive_label")
    @patch("scan_and_fix.has_commits_ahead", return_value=False)
    @patch("scan_and_fix.get_branch_sha", return_value="abc1234")
    @patch("scan_and_fix.gemini_api")
    @patch("scan_and_fix.github_api")
    @patch("scan_and_fix.find_items_by_label")
    def test_check_wip_items_finds_tracking_id_beyond_20_comments(
        self,
        mock_find,
        mock_gh,
        mock_gemini,
        _mock_sha,
        _mock_ahead,
        mock_set_label,
        mock_post,
    ):
        mock_find.return_value = [
            {
                "repository_url": "https://api.github.com/repos/owner/repo",
                "number": 99,
                "title": "Long thread",
                "labels": [{"name": scan_and_fix.RUNNING_LABEL}],
            }
        ]
        # 25 comments (first 24 without marker, 25th with marker)
        comments = [{"body": f"comment {i}"} for i in range(24)]
        comments.append({"body": "⏳\n<!-- antigravity:id=task-99;sha=deadbeef -->"})
        mock_gh.side_effect = [
            comments,  # get_all_issue_comments (page 1, < 100)
            {"default_branch": "main"},  # repo_info
        ]
        mock_gemini.return_value = {
            "status": "completed",
            "outputs": [{"text": "要約: 調査のみ完了しました。"}],
        }

        has_err, count = scan_and_fix.check_wip_items("owner", "gkey", "gtok")
        self.assertFalse(has_err)
        self.assertEqual(count, 1)
        mock_set_label.assert_called_once()
        mock_post.assert_called_once()

    def test_select_auto_improve_repo_rotates_every_15_minutes(self):
        candidates = [{"full_name": "a/1"}, {"full_name": "a/2"}, {"full_name": "a/3"}, {"full_name": "a/4"}]
        picked = [
            scan_and_fix.select_auto_improve_repo(
                candidates, datetime(2026, 11, 18, 10, m, tzinfo=scan_and_fix.JST)
            )["full_name"]
            for m in (0, 15, 30, 45)
        ]
        self.assertEqual(len(set(picked)), 4)

    @patch("scan_and_fix.post_comment")
    @patch("scan_and_fix.review_pull_request")
    @patch("scan_and_fix.ensure_pr_exists", return_value=("https://github.com/owner/app/pull/5", 5))
    @patch("scan_and_fix.has_commits_ahead", return_value=True)
    @patch("scan_and_fix.get_branch_sha", return_value="beef1234")
    @patch("scan_and_fix.gemini_api")
    @patch("scan_and_fix.get_all_issue_comments")
    @patch("scan_and_fix.find_tracker_issue", return_value=12)
    def test_check_pending_auto_improve_recovers_and_creates_draft_pr(
        self,
        _mock_tracker,
        mock_comments,
        mock_gemini,
        _mock_sha,
        _mock_ahead,
        mock_ensure_pr,
        mock_review,
        mock_post,
    ):
        mock_comments.return_value = [
            {
                "body": "⏳\n<!-- antigravity:auto-improve-wip;id=ai-77;repo=owner/app;base=main;branch=chore/auto-improve-20261118 -->"
            }
        ]
        mock_gemini.return_value = {
            "status": "completed",
            "outputs": [{"text": "タイトル: テスト追加\n要約: エッジケーステストを追加"}],
        }

        has_err, still_running = scan_and_fix.check_pending_auto_improve("owner", "gkey", "gtok")
        self.assertFalse(has_err)
        self.assertFalse(still_running)
        mock_ensure_pr.assert_called_once()
        self.assertTrue(mock_ensure_pr.call_args.kwargs.get("draft"))
        mock_review.assert_called_once_with("owner/app", 5, "gkey", "gtok")
        self.assertIn("<!-- antigravity:auto-improve-done;id=ai-77 -->", mock_post.call_args.args[2])

    @patch("scan_and_fix.gemini_api")
    @patch("scan_and_fix.get_all_issue_comments")
    @patch("scan_and_fix.find_tracker_issue", return_value=12)
    def test_check_pending_auto_improve_skips_when_already_done(
        self,
        _mock_tracker,
        mock_comments,
        mock_gemini,
    ):
        mock_comments.return_value = [
            {"body": "<!-- antigravity:auto-improve-wip;id=ai-77;repo=owner/app;base=main;branch=chore/b -->"},
            {"body": "<!-- antigravity:auto-improve-done;id=ai-77 -->"},
        ]
        has_err, still_running = scan_and_fix.check_pending_auto_improve("owner", "gkey", "gtok")
        self.assertFalse(has_err)
        self.assertFalse(still_running)
        mock_gemini.assert_not_called()

    def test_prompts_use_force_with_lease_and_no_secret_leak_rule(self):
        p_issue = scan_and_fix.build_prompt(
            False, "owner/app", "main", "fix/issue-1", 1, "t", "b", "", "", "gtok"
        )
        p_auto = scan_and_fix.build_auto_improve_prompt("owner/app", "main", "chore/auto", "gtok")
        for p in (p_issue, p_auto):
            self.assertIn("--force-with-lease", p)
            self.assertIn("git fetch origin", p)
            self.assertIn("NEVER print, log, or commit access tokens", p)


if __name__ == "__main__":
    unittest.main()
