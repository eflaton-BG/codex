#!/usr/bin/env python3
"""Collect repo-scoped GitHub PR evidence for a person.

This avoids flaky org-wide search behavior in private repos by querying each
repo with `gh pr list`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


DEFAULT_REPOS = [
    "berkshiregrey/bg_breakpack",
    "berkshiregrey/bg_breakpack_deployment",
    "berkshiregrey/bg_washington_breakpack",
    "berkshiregrey/bg_britton_breakpack",
    "berkshiregrey/bg_huron_breakpack",
    "berkshiregrey/bg_maunakea_breakpack",
    "berkshiregrey/bg_sunflower_breakpack",
    "berkshiregrey/bg_core",
    "berkshiregrey/bg_common",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True, help="GitHub login")
    parser.add_argument(
        "--since",
        default="2026-01-01",
        help="Lower date bound in YYYY-MM-DD format for GitHub search",
    )
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Optional repo override. Repeat for multiple repos.",
    )
    return parser.parse_args()


def gh_pr_list(repo: str, search: str) -> list[dict]:
    cmd = [
        "/usr/bin/gh",
        "pr",
        "list",
        "-R",
        repo,
        "--state",
        "all",
        "--search",
        search,
        "--json",
        "number,title,url,createdAt,updatedAt,state,additions,deletions,author",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh failed for {repo}")
    return json.loads(proc.stdout)


def collect_category(repos: list[str], label: str, qualifier: str, user: str, since: str) -> list[dict]:
    items: list[dict] = []
    for repo in repos:
        search = f"{qualifier}:{user} updated:>={since}"
        try:
            repo_items = gh_pr_list(repo, search)
        except Exception as exc:  # noqa: BLE001
            items.append({"repo": repo, "category": label, "error": str(exc)})
            continue
        for item in repo_items:
            item["repo"] = repo
            item["category"] = label
            items.append(item)
    return items


def dedupe_review_items(items: list[dict], user: str) -> list[dict]:
    deduped: list[dict] = []
    seen: set[tuple[str, int, str]] = set()
    for item in items:
        if "error" in item:
            deduped.append(item)
            continue
        author = (item.get("author") or {}).get("login")
        if author == user and item.get("category") in {"reviewed", "commented"}:
            continue
        key = (item["repo"], item["number"], item["category"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def main() -> int:
    args = parse_args()
    repos = args.repos or DEFAULT_REPOS

    authored = collect_category(repos, "authored", "author", args.user, args.since)
    reviewed = collect_category(repos, "reviewed", "reviewed-by", args.user, args.since)
    commented = collect_category(repos, "commented", "commenter", args.user, args.since)

    data = {
        "user": args.user,
        "since": args.since,
        "repos": repos,
        "authored": dedupe_review_items(authored, args.user),
        "reviewed": dedupe_review_items(reviewed, args.user),
        "commented": dedupe_review_items(commented, args.user),
    }
    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
