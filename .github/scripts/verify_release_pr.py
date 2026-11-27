"""Verify the PR authorization for the exact commit being released."""

import json
import os
import re
import subprocess
from urllib.parse import quote


def github(path, *, paginate=False):
    command = ["gh", "api"]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    response = subprocess.check_output([*command, path], text=True)
    result = json.loads(response)
    return [item for page in result for item in page] if paginate else result


def verify(repository, sha, api=github):
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Expected the full release commit SHA")
    repo = f"repos/{repository}"
    if api(f"{repo}/branches/main")["protected"] is not True:
        raise ValueError("main must be protected")
    pulls = api(f"{repo}/commits/{sha}/pulls?per_page=100", paginate=True)
    matches = [
        pr for pr in pulls
        if pr["merged_at"]
        and pr["merge_commit_sha"] == sha
        and pr["base"]["ref"] == "main"
        and pr["base"]["repo"]["full_name"].casefold() == repository.casefold()
    ]
    if len(matches) != 1:
        raise ValueError("Expected exactly one PR merged into main at the release SHA")
    pr = matches[0]
    author_id = pr["user"]["id"]
    if type(author_id) is not int or author_id <= 0 or not re.fullmatch(r"[a-f0-9]{40}", pr["head"]["sha"]):
        raise ValueError("Missing PR author or final head SHA")
    reviews = api(f"{repo}/pulls/{pr['number']}/reviews?per_page=100", paginate=True)
    # GitHub returns chronological reviews. Comments do not replace a review decision.
    # GitHub 按时间返回评审。普通评论不替换已有的评审决定。
    decisions = {}
    for review in reviews:
        user = review.get("user") or {}
        reviewer_id = user.get("id")
        if (
            user.get("type") == "User"
            and type(reviewer_id) is int and reviewer_id > 0 and reviewer_id != author_id
            and review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
            and review["submitted_at"] and review["submitted_at"] <= pr["merged_at"]
        ):
            decisions[reviewer_id] = review
    approvers = []
    for review in decisions.values():
        if review["state"] == "DISMISSED":
            continue
        user = review["user"]
        access = api(f"{repo}/collaborators/{quote(user['login'], safe='')}/permission")
        if access["permission"] not in {"write", "admin"} or access["user"]["id"] != user["id"]:
            continue
        if review["state"] == "CHANGES_REQUESTED":
            raise ValueError("A reviewer with write access still requested changes")
        if review["commit_id"] == pr["head"]["sha"]:
            approvers.append(user["login"])
    if not approvers:
        raise ValueError("Missing independent approval of the final PR head by a repository writer")
    print(f"Verified PR #{pr['number']} for {sha} (approved by {', '.join(approvers)})")


if __name__ == "__main__":
    verify(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"])
