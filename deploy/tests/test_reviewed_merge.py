"""Offline authorization checks with GitHub REST response fixtures."""

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".github/scripts"))
import verify_release_pr


class ReviewedMergeTests(unittest.TestCase):
    repo = "team/agenthub"
    sha = "a" * 40
    head = "b" * 40

    def setUp(self):
        self.pr = dict(number=17, base=dict(ref="main", repo=dict(full_name=self.repo)),
                       head=dict(sha=self.head), user=dict(id=1), merge_commit_sha=self.sha,
                       merged_at="2026-11-20T09:00:00Z")
        self.review = dict(state="APPROVED", user=dict(id=2, login="reviewer", type="User"),
                           submitted_at="2026-11-20T08:00:00Z", commit_id=self.head)
        self.branch = dict(protected=True)
        self.access = dict(permission="write", user=dict(id=2))
        self.pulls = [self.pr]
        self.reviews = [self.review]

    def api(self, path, *, paginate=False):
        return {
            ("branches/main", False): self.branch,
            (f"commits/{self.sha}/pulls?per_page=100", True): self.pulls,
            ("pulls/17/reviews?per_page=100", True): self.reviews,
            ("collaborators/reviewer/permission", False): self.access,
        }[path.removeprefix(f"repos/{self.repo}/"), paginate]

    def verify(self):
        verify_release_pr.verify(self.repo, self.sha, self.api)

    def test_approved_final_head_can_have_later_comments(self):
        self.reviews.append(dict(self.review, state="COMMENTED"))
        self.verify()

    def test_branch_protection_direct_push_ambiguous_and_wrong_merge(self):
        self.branch["protected"] = False
        with self.assertRaisesRegex(ValueError, "protected"):
            self.verify()
        self.branch["protected"] = True
        for pulls in ([], [self.pr, self.pr], [dict(self.pr, merged_at=None)],
                      [dict(self.pr, merge_commit_sha=self.head)],
                      [dict(self.pr, base=dict(ref="other", repo=dict(full_name=self.repo)))],
                      [dict(self.pr, base=dict(ref="main", repo=dict(full_name="fork/agenthub")))]):
            self.pulls = pulls
            with self.assertRaisesRegex(ValueError, "exactly one"):
                self.verify()

    def test_missing_stale_late_self_bot_and_revoked_reviews(self):
        for reviews in ([], [dict(self.review, commit_id=self.sha)],
                        [dict(self.review, submitted_at="2026-11-21T00:00:00Z")],
                        [dict(self.review, user=dict(id=1, type="User"))],
                        [dict(self.review, user=dict(id=2, type="Bot"))],
                        [self.review, dict(self.review, state="DISMISSED")],
                        [self.review, dict(self.review, state="CHANGES_REQUESTED")]):
            self.reviews = reviews
            with self.assertRaises(ValueError):
                self.verify()

    def test_review_requires_writer_permission_and_same_user_id(self):
        for access in (dict(permission="read", user=dict(id=2)), dict(permission="write", user=dict(id=3))):
            self.access = access
            with self.assertRaisesRegex(ValueError, "independent approval"):
                self.verify()

    def test_page_boundaries_and_api_failure(self):
        pages = [[self.review], [dict(self.review, state="DISMISSED")]]
        with patch.object(subprocess, "check_output", return_value=json.dumps(pages)) as command:
            self.assertEqual(verify_release_pr.github("endpoint", paginate=True), pages[0] + pages[1])
            command.assert_called_once_with(["gh", "api", "--paginate", "--slurp", "endpoint"], text=True)
        with patch.object(subprocess, "check_output", side_effect=subprocess.CalledProcessError(1, "gh")):
            with self.assertRaises(subprocess.CalledProcessError):
                verify_release_pr.verify(self.repo, self.sha)
