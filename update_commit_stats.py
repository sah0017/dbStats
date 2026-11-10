#!/usr/bin/env python3
"""
update_commit_stats.py - Write a commitStats.md file into every git repository
in a folder, then commit and push it.

For each repository the script:
  1. checks it is on a branch with an upstream and has no uncommitted changes
     (otherwise the repo is skipped, never modified),
  2. pulls the latest commits (fast-forward only),
  3. computes per-student stats with git_authorship.py: commits, spec lines and
     code lines in the current files, lines added over the history, and (from
     GitHub, via the `gh` CLI) pull requests opened, merged and reviewed.
     Instructor contributions are excluded and AI-agent commits are credited
     to the requesting student (see AGENTS.md),
  4. checks each feature spec's owner (specs/features/*.md) against who wrote
     the spec and the feature's code,
  5. writes commitStats.md, commits only that file, and pushes.

Usage:
  python3 update_commit_stats.py                       # all repos in the default folder
  python3 update_commit_stats.py /path/to/folder
  python3 update_commit_stats.py --repo Fall26RecipeMgmt --repo Fall26Forensics
  python3 update_commit_stats.py --dry-run             # print stats; change nothing
  python3 update_commit_stats.py --no-push             # commit locally but don't push
  python3 update_commit_stats.py --no-github           # skip pull request stats
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import git_authorship as ga

DEFAULT_FOLDER = "/Users/sahammond/Fall26DB"
STATS_FILE = "commitStats.md"

# Standard options from AGENTS.md: exclude the instructor, credit AI agents to
# the requesting student, skip the committed virtualenv and the stats file itself.
EXCLUDE_AUTHORS = ["sahammond@lipscomb.edu", "sah0017"]
EXCLUDE_PATHS = ["lib/*", "bin/*", "pyvenv.cfg", STATS_FILE]
# GitHub accounts left out of pull request stats (the instructor)
EXCLUDE_LOGINS = {"sah0017"}

SPEC_GLOB = "specs/*"
FEATURE_SPEC_DIR = "specs/features"
# Flag a feature when its owner wrote less than this share of its code
OWNER_SHARE_WARNING = 50.0


def git(repo: str, *args: str) -> str:
    """Run a git command that changes the repo (no mailmap needed)."""
    proc = subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise ga.GitError((proc.stderr or proc.stdout).strip())
    return proc.stdout.strip()


def author_args(repo: str, mode: str) -> argparse.Namespace:
    argv = [repo, "--mode", mode, "--credit-bots", "--no-progress"]
    for a in EXCLUDE_AUTHORS:
        argv += ["--exclude-author", a]
    for p in EXCLUDE_PATHS:
        argv += ["--exclude", p]
    return ga.parse_args(argv)


def matches_any(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in globs)


# --------------------------------------------------------------------------- #
# git stats
# --------------------------------------------------------------------------- #

def blame_by_file(args) -> dict[str, Counter]:
    """Blame every text file once; return {path: Counter(author -> lines)}."""
    files = [f for f in ga.list_text_files(args.repo, args.rev)
             if ga.path_selected(f, args.include, args.exclude)]
    credit = ga.bot_credit_map(args.repo, args.rev)
    per_file: dict[str, Counter] = {}
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(ga.blame_file, args.repo, args.rev, f, args.by, [],
                               args.exclude_author, credit): f for f in files}
        for fut, path in futures.items():
            try:
                per_file[path] = fut.result()[0]
            except ga.GitError as e:
                print(f"warning: skipping {path}: {e}", file=sys.stderr)
    return per_file


def sum_files(per_file: dict[str, Counter], keep) -> Counter:
    total: Counter = Counter()
    for path, counts in per_file.items():
        if keep(path):
            total.update(counts)
    return total


# --------------------------------------------------------------------------- #
# feature specs
# --------------------------------------------------------------------------- #

def parse_front_matter(text: str) -> dict:
    """Read the simple `key: value` / `- item` header between --- lines."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    data: dict = {}
    key = None
    for raw in text[3:end].splitlines():
        line = re.sub(r"\s+#.*$", "", raw).rstrip()   # drop trailing comments
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and key:
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(stripped[2:].strip())
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            data[key] = value.strip() if value.strip() else []
    return data


def load_features(repo: str) -> list[dict]:
    folder = os.path.join(repo, FEATURE_SPEC_DIR)
    if not os.path.isdir(folder):
        return []
    features = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".md"):
            continue
        with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
            meta = parse_front_matter(f.read())
        paths = meta.get("paths") or []
        features.append({
            "spec": f"{FEATURE_SPEC_DIR}/{name}",
            "number": name.split("-", 1)[0] if name[:2].isdigit() else "",
            "name": meta.get("feature") or name[:-3],
            "owner": meta.get("owner") if isinstance(meta.get("owner"), str) else "",
            "status": meta.get("status") if isinstance(meta.get("status"), str) else "",
            "paths": paths if isinstance(paths, list) else [paths],
        })
    return features


def is_owner(author_key: str, owner: str) -> bool:
    """Does a 'Name <email>' author key belong to the spec's owner?"""
    if not owner:
        return False
    name, email = ga.parse_identity(author_key)
    owner = owner.strip().lower()
    return owner in (email.lower(), name.lower())


def feature_rows(features: list[dict], per_file: dict[str, Counter]) -> list[dict]:
    rows = []
    for feat in features:
        if feat["owner"] and ga.author_excluded("", feat["owner"], EXCLUDE_AUTHORS):
            continue  # an instructor example, such as 01-actors
        spec = per_file.get(feat["spec"], Counter())
        code = sum_files(per_file, lambda p: matches_any(p, feat["paths"]))
        spec_total, code_total = sum(spec.values()), sum(code.values())
        spec_owner = sum(n for a, n in spec.items() if is_owner(a, feat["owner"]))
        code_owner = sum(n for a, n in code.items() if is_owner(a, feat["owner"]))
        others = [(a, n) for a, n in code.most_common() if not is_owner(a, feat["owner"])]

        notes = []
        if not feat["owner"]:
            notes.append("no owner in spec header")
        elif spec_total and spec_owner * 2 < spec_total:
            notes.append("spec mostly written by someone else")
        if not feat["paths"]:
            notes.append("no paths in spec header")
        elif not code_total:
            notes.append("no code yet")
        elif 100.0 * code_owner / code_total < OWNER_SHARE_WARNING:
            notes.append("owner wrote under half the code")
        rows.append({**feat, "spec_total": spec_total, "spec_owner": spec_owner,
                     "code_total": code_total, "code_owner": code_owner,
                     "others": others, "notes": notes})
    return rows


# --------------------------------------------------------------------------- #
# GitHub pull requests
# --------------------------------------------------------------------------- #

def github_slug(repo: str) -> str | None:
    try:
        url = git(repo, "remote", "get-url", "origin")
    except ga.GitError:
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def gh_lines(*args: str) -> list[str]:
    proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh api failed")
    return [l for l in proc.stdout.splitlines() if l.strip()]


def is_bot_login(login: str) -> bool:
    return login.endswith("[bot]") or login.lower() == "copilot"


def pull_request_stats(repo: str) -> tuple[dict[str, Counter], dict[str, str]] | str:
    """Return ({'opened'|'merged'|'reviewed': Counter(author key)}, unmatched)
    or an error string. Logins are matched to commit identities through the
    repo's commits on GitHub and the mailmap."""
    slug = github_slug(repo)
    if not slug:
        return "the repository's origin isn't on GitHub"
    try:
        prs = [json.loads(l) for l in gh_lines(
            "--paginate", f"repos/{slug}/pulls?state=all&per_page=100",
            "--jq", ".[] | {number, login: .user.login, merged: (.merged_at != null)}")]
        reviewed: dict[int, set[str]] = defaultdict(set)
        for pr in prs:
            for login in gh_lines("--paginate", f"repos/{slug}/pulls/{pr['number']}/reviews",
                                  "--jq", ".[] | .user.login // empty"):
                if login != pr["login"]:
                    reviewed[pr["number"]].add(login)
        commit_ids = [l.split("\t") for l in gh_lines(
            "--paginate", f"repos/{slug}/commits?per_page=100",
            "--jq", '.[] | select(.author != null) | [.author.login, .commit.author.name, .commit.author.email] | @tsv')]
    except (RuntimeError, FileNotFoundError) as e:
        return f"GitHub lookup failed ({e})"

    # GitHub login -> the commit identity it most often uses, after the mailmap
    seen: dict[str, Counter] = defaultdict(Counter)
    for parts in commit_ids:
        if len(parts) == 3:
            seen[parts[0]][f"{parts[1]} <{parts[2]}>"] += 1
    login_key = {}
    raw = sorted({ident.most_common(1)[0][0] for ident in seen.values()})
    if raw:
        mapped = ga.run_git(repo, "check-mailmap", *raw).decode("utf-8", "replace").splitlines()
        canon = dict(zip(raw, mapped))
        for login, ident in seen.items():
            n, e = ga.parse_identity(canon.get(ident.most_common(1)[0][0], ""))
            login_key[login] = ga.author_key(n, e, "both")

    def key_for(login):
        return login_key.get(login, login)

    def skip(login):
        if login in EXCLUDE_LOGINS or is_bot_login(login):
            return True
        n, e = ga.parse_identity(key_for(login))
        return ga.author_excluded(n, e, EXCLUDE_AUTHORS)

    stats = {"opened": Counter(), "merged": Counter(), "reviewed": Counter()}
    for pr in prs:
        if not skip(pr["login"]):
            stats["opened"][key_for(pr["login"])] += 1
            stats["merged"][key_for(pr["login"])] += pr["merged"]
        for login in reviewed[pr["number"]]:
            if not skip(login):
                stats["reviewed"][key_for(login)] += 1
    unmatched = {login: login for login in
                 {pr["login"] for pr in prs} | {l for s in reviewed.values() for l in s}
                 if login not in login_key and not skip(login)}
    return stats, unmatched


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def collect_stats(repo: str, use_github: bool) -> dict:
    blame_args = author_args(repo, "blame")
    ga.configure_mailmap(blame_args)
    per_file = blame_by_file(blame_args)
    history, _, _ = ga.count_by_log(author_args(repo, "log"))
    commits, _ = ga.count_commits(blame_args)
    prs = pull_request_stats(repo) if use_github else "skipped (--no-github)"
    return {
        "specs": sum_files(per_file, lambda p: fnmatch.fnmatch(p, SPEC_GLOB)),
        "code": sum_files(per_file, lambda p: not fnmatch.fnmatch(p, SPEC_GLOB)),
        "history": history,
        "commits": commits,
        "prs": prs,
        "features": feature_rows(load_features(repo), per_file),
    }


def cell(n: int, total: int) -> str:
    return f"{n:,} ({100.0 * n / total:.1f}%)" if total else f"{n:,}"


def split_key(key: str) -> tuple[str, str]:
    name, email = ga.parse_identity(key)
    return name or key, email


def render(repo: str, stats: dict, when: datetime) -> str:
    specs, code, history, commits = (stats[k] for k in ("specs", "code", "history", "commits"))
    pr_ok = isinstance(stats["prs"], tuple)
    prs = stats["prs"][0] if pr_ok else {"opened": Counter(), "merged": Counter(), "reviewed": Counter()}
    head = git(repo, "rev-parse", "--short", "HEAD")
    branch = git(repo, "branch", "--show-current")
    tot_spec, tot_code, tot_hist, tot_com = (sum(c.values()) for c in (specs, code, history, commits))

    authors = set(specs) | set(code) | set(history) | set(commits)
    for c in prs.values():
        authors |= set(c)
    authors = sorted(authors, key=lambda a: (-(specs[a] + code[a]), -history[a], -commits[a], a))

    lines = [
        "# Commit Stats",
        "",
        f"**Repository:** {os.path.basename(os.path.abspath(repo))}  ",
        f"**Updated:** {when.strftime('%A, %B %-d, %Y at %-I:%M %p %Z')}  ",
        f"**As of commit:** `{head}` on `{branch}`",
        "",
        "Contributions by each student. Instructor-provided base code is excluded,",
        "and work done by AI coding agents (such as Copilot's coding agent) is credited",
        "to the student who requested it.",
        "",
        "## Students",
        "",
    ]
    if not authors:
        lines += ["_No student contributions yet._", ""]
    else:
        header = "| Student | Email | Commits | Spec lines | Code lines | Lines added (all history) |"
        rule = "|---|---|--:|--:|--:|--:|"
        if pr_ok:
            header += " PRs opened | PRs merged | PRs reviewed |"
            rule += "--:|--:|--:|"
        lines += [header, rule]
        for a in authors:
            name, email = split_key(a)
            row = (f"| {name} | {email} | {commits[a]} | {cell(specs[a], tot_spec)} "
                   f"| {cell(code[a], tot_code)} | {cell(history[a], tot_hist)} |")
            if pr_ok:
                row += f" {prs['opened'][a]} | {prs['merged'][a]} | {prs['reviewed'][a]} |"
            lines.append(row)
        total = (f"| **Total** | | **{tot_com}** | **{tot_spec:,}** | **{tot_code:,}** "
                 f"| **{tot_hist:,}** |")
        if pr_ok:
            total += (f" **{sum(prs['opened'].values())}** | **{sum(prs['merged'].values())}** "
                      f"| **{sum(prs['reviewed'].values())}** |")
        lines += [total, ""]
    if not pr_ok:
        lines += [f"_Pull request stats unavailable: {stats['prs']}._", ""]

    features = stats["features"]
    lines += ["## Features", ""]
    if not features:
        lines += [f"_No feature specs in `{FEATURE_SPEC_DIR}/` yet._", ""]
    else:
        lines += [
            "Each feature's owner (from its spec's header) compared with who wrote its spec and its code (the files listed in the spec's `paths`).",
            "",
            "| # | Feature | Owner | Status | Spec by owner | Code lines | Code by owner | Other contributors | Check |",
            "|---|---|---|---|--:|--:|--:|---|---|",
        ]
        for f in features:
            others = ", ".join(f"{split_key(a)[0]} ({n:,})" for a, n in f["others"]) or "-"
            spec_cell = (f"{100.0 * f['spec_owner'] / f['spec_total']:.0f}%"
                         if f["spec_total"] and f["owner"] else "-")
            code_cell = (f"{100.0 * f['code_owner'] / f['code_total']:.0f}%"
                         if f["code_total"] else "-")
            lines.append(
                f"| {f['number']} | [{f['name']}]({f['spec']}) | {f['owner'] or '-'} "
                f"| {f['status'] or '-'} | {spec_cell} | {f['code_total']:,} | {code_cell} "
                f"| {others} | {'; '.join(f['notes']) or 'OK'} |")
        lines.append("")

    lines += [
        "## How to read this",
        "",
        "- *Spec lines* and *Code lines*: lines that exist now, credited to whoever last changed them (`git blame`). Spec lines are files in `specs/`; code lines are every other file.",
        "- *Lines added (all history)*: every line each student added in any commit, including lines later changed or deleted.",
        "- *Commits*: non-merge commits.",
        "- *PRs reviewed*: teammates' pull requests on which the student left a review on GitHub.",
        f"- *Check*: flags a feature with no owner or paths in its spec header, a spec mostly written by someone else, no code yet, or an owner who wrote under {OWNER_SHARE_WARNING:.0f}% of its code.",
        "",
        "_Generated by `update_commit_stats.py`. This file is regenerated on each run; don't edit it by hand._",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# per-repo workflow
# --------------------------------------------------------------------------- #

def preflight(repo: str) -> str | None:
    """Return a reason to skip this repo, or None if it's safe to update."""
    if not git(repo, "branch", "--show-current"):
        return "not on a branch (detached HEAD)"
    try:
        git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    except ga.GitError:
        return "current branch has no upstream to push to"
    dirty = [l for l in git(repo, "status", "--porcelain").splitlines()
             if l[3:].strip('"') != STATS_FILE]
    if dirty:
        return f"has uncommitted changes ({len(dirty)} file(s))"
    return None


def process(repo: str, args, when: datetime) -> str:
    # A dry run changes nothing, so it doesn't need the safety checks
    reason = None if args.dry_run else preflight(repo)
    if reason:
        return f"SKIPPED: {reason}"

    if not args.no_pull:
        try:
            git(repo, "pull", "--ff-only", "--quiet")
        except ga.GitError as e:
            return f"SKIPPED: pull failed ({str(e).splitlines()[-1] if str(e) else e})"

    content = render(repo, collect_stats(repo, not args.no_github), when)

    if args.dry_run:
        print(content)
        return "dry run: nothing written"

    path = os.path.join(repo, STATS_FILE)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    git(repo, "add", "--", STATS_FILE)
    if not git(repo, "diff", "--cached", "--name-only", "--", STATS_FILE):
        return "unchanged"
    git(repo, "commit", "--quiet", "-m",
        f"Update commit stats ({when.strftime('%Y-%m-%d %H:%M %Z')})",
        "--", STATS_FILE)
    sha = git(repo, "rev-parse", "--short", "HEAD")

    if args.no_push:
        return f"committed {sha} (not pushed)"
    try:
        git(repo, "push", "--quiet")
    except ga.GitError as e:
        return f"committed {sha} but PUSH FAILED: {e}"
    return f"committed and pushed {sha}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Write, commit and push commitStats.md "
                                            "in every git repo in a folder.")
    p.add_argument("folder", nargs="?", default=DEFAULT_FOLDER,
                   help=f"folder containing the repos (default: {DEFAULT_FOLDER})")
    p.add_argument("--repo", action="append", default=[], metavar="NAME",
                   help="only update this repo (folder name); repeatable")
    p.add_argument("--dry-run", action="store_true",
                   help="print each repo's stats; don't write, commit, pull or push")
    p.add_argument("--no-push", action="store_true", help="commit but don't push")
    p.add_argument("--no-pull", action="store_true", help="don't pull before computing stats")
    p.add_argument("--no-github", action="store_true",
                   help="skip pull request stats (no GitHub API calls)")
    args = p.parse_args(argv)
    if args.dry_run:
        args.no_pull = True

    folder = os.path.abspath(args.folder)
    repos = sorted(d for d in os.listdir(folder)
                   if os.path.isdir(os.path.join(folder, d, ".git")))
    if args.repo:
        missing = set(args.repo) - set(repos)
        if missing:
            print(f"error: not git repos in {folder}: {', '.join(sorted(missing))}",
                  file=sys.stderr)
            return 1
        repos = [r for r in repos if r in args.repo]
    if not repos:
        print(f"No git repositories found in {folder}", file=sys.stderr)
        return 1

    when = datetime.now().astimezone()
    results = {}
    for name in repos:
        print(f"== {name}", file=sys.stderr)
        try:
            results[name] = process(os.path.join(folder, name), args, when)
        except ga.GitError as e:
            results[name] = f"ERROR: {e}"

    print("\nSummary", file=sys.stderr)
    width = max(len(r) for r in results)
    for name, result in results.items():
        print(f"  {name:<{width}}  {result}", file=sys.stderr)
    failed = any(r.startswith(("ERROR", "SKIPPED")) or "FAILED" in r
                 for r in results.values())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
