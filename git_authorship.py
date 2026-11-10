#!/usr/bin/env python3
"""
git_authorship.py - Report what percentage of a git repository's lines of code
were written by each contributor, using only the git command line.

Two counting modes are available:

  blame (default)  Runs `git blame` on every text file at a revision and credits
                   each line that exists *now* to the author who last wrote it.
                   Answers: "Who wrote the code as it currently stands?"

  log              Sums the lines added in every commit (`git log --numstat`).
                   Answers: "Who has written the most lines over all of history?"
                   (includes lines later deleted or rewritten).

Examples:
  python3 git_authorship.py                      # current directory, HEAD
  python3 git_authorship.py ~/src/myrepo
  python3 git_authorship.py --rev v2.0 --exclude '*.json' --exclude 'vendor/*'
  python3 git_authorship.py --include '*.py' --by name
  python3 git_authorship.py --mode log --since 2025-01-01
  python3 git_authorship.py --exclude-author sahammond@lipscomb.edu
  python3 git_authorship.py --format csv > authors.csv
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

# The hash of git's empty tree; diffing against it lists every file at a revision.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# First line of each `git blame --porcelain` entry: "<sha> <orig> <final>[ <n>]"
SHA_HEADER = re.compile(rb"^[0-9a-f]{40} \d+ \d+")


class GitError(RuntimeError):
    pass


# Extra `-c key=value` options passed to every git command (set by --mailmap).
GIT_CONFIG: list[str] = []

# Default identity map: authors.mailmap next to this script, if present.
DEFAULT_MAILMAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "authors.mailmap")


def run_git(repo: str, *args: str) -> bytes:
    """Run a git command in `repo` and return raw stdout (bytes)."""
    try:
        proc = subprocess.run(
            ["git", *GIT_CONFIG, "-C", repo, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        raise GitError("git executable not found on PATH")
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode(errors="replace").strip())
    return proc.stdout


def path_selected(path: str, includes: list[str], excludes: list[str]) -> bool:
    """Apply --include / --exclude glob filters (matched against the full path
    and the base filename)."""
    name = os.path.basename(path)

    def matches(pattern: str) -> bool:
        return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern)

    if includes and not any(matches(p) for p in includes):
        return False
    return not any(matches(p) for p in excludes)


def author_excluded(name: str, email: str, patterns: list[str]) -> bool:
    """True if the author's name or email matches any --exclude-author glob
    (case-insensitive)."""
    name, email = name.lower(), email.lower()
    return any(fnmatch.fnmatchcase(name, p.lower()) or fnmatch.fnmatchcase(email, p.lower())
               for p in patterns)


def is_ai_identity(name: str, email: str) -> bool:
    """Heuristic: does this author/co-author identity belong to a bot or AI agent?"""
    name, email = name.lower(), email.lower()
    return (name.endswith("[bot]")
            or email == "noreply@anthropic.com"
            or "copilot" in name or "copilot" in email)


def parse_identity(value: str) -> tuple[str, str]:
    """Split 'Name <email>' into (name, email)."""
    name, _, rest = value.strip().partition("<")
    return name.strip(), rest.rstrip(">").strip()


def bot_credit_map(repo: str, rev: str) -> dict[str, tuple[str, str]]:
    """Map commit hash -> (name, email) of the human to credit, for commits
    authored by a bot that name a human in a `Co-authored-by:` trailer.

    Cloud coding agents (e.g. GitHub's Copilot coding agent) author commits as
    a bot and add the requesting user as a co-author; this lets that user get
    the credit. Bot commits with no human co-author are left alone.
    """
    out = run_git(repo, "log", rev,
                  "--format=%H%x00%aN%x00%aE%x00"
                  "%(trailers:key=Co-authored-by,valueonly,separator=%x01)%x1e")
    credit = {}
    for record in out.decode("utf-8", errors="replace").split("\x1e"):
        fields = record.strip("\n").split("\0")
        if len(fields) != 4:
            continue
        commit, name, email, trailers = fields
        if not is_ai_identity(name, email):
            continue
        for value in trailers.split("\x01"):
            co_name, co_email = parse_identity(value)
            if co_name and not is_ai_identity(co_name, co_email):
                credit[commit] = (co_name, co_email)
                break
    if not credit:
        return credit

    # Trailer identities aren't mailmapped by git log/blame; map them explicitly
    # so a student's GitHub username merges with their usual identity.
    contacts = sorted({f"{n} <{e}>" for n, e in credit.values()})
    mapped_out = run_git(repo, "check-mailmap", *contacts).decode("utf-8", errors="replace")
    mapped = dict(zip(contacts, (parse_identity(l) for l in mapped_out.splitlines())))
    return {c: mapped.get(f"{n} <{e}>", (n, e)) for c, (n, e) in credit.items()}


def author_key(name: str, email: str, by: str) -> str:
    if by == "name":
        return name
    if by == "email":
        return email
    return f"{name} <{email}>"


# --------------------------------------------------------------------------- #
# blame mode
# --------------------------------------------------------------------------- #

def list_text_files(repo: str, rev: str) -> list[str]:
    """Return the text (non-binary) files present at `rev`.

    `git diff --numstat <empty-tree> <rev>` lists every file in the revision;
    binary files show "-" for their line counts, which lets us skip them.
    """
    out = run_git(repo, "diff", "--numstat", "-z", "--no-renames", EMPTY_TREE, rev)
    files = []
    for record in out.split(b"\0"):
        if not record:
            continue
        added, _deleted, path = record.split(b"\t", 2)
        if added == b"-":  # binary
            continue
        files.append(path.decode("utf-8", errors="surrogateescape"))
    return files


def blame_file(repo: str, rev: str, path: str, by: str, blame_opts: list[str],
               exclude_authors: list[str],
               credit: dict[str, tuple[str, str]]) -> tuple[Counter, int]:
    """Count lines per author in one file via `git blame --line-porcelain`.
    Returns (per-author counts, number of lines by excluded authors)."""
    counts: Counter = Counter()
    excluded = 0
    out = run_git(repo, "blame", "--line-porcelain", *blame_opts, rev, "--", path)
    commit = name = email = ""
    for line in out.split(b"\n"):
        if line.startswith(b"\t"):          # the actual source line
            if commit in credit:
                name, email = credit[commit]
            if author_excluded(name, email, exclude_authors):
                excluded += 1
            else:
                counts[author_key(name, email, by)] += 1
        elif line.startswith(b"author "):
            name = line[7:].decode("utf-8", errors="replace")
        elif SHA_HEADER.match(line):
            # Header line: "<sha> <orig-line> <final-line> [<count>]"
            commit = line[:40].decode("ascii")
        elif line.startswith(b"author-mail "):
            email = line[12:].decode("utf-8", errors="replace").strip("<>")
    return counts, excluded


def count_by_blame(args) -> tuple[Counter, int, int]:
    files = [f for f in list_text_files(args.repo, args.rev)
             if path_selected(f, args.include, args.exclude)]

    blame_opts = []
    if args.ignore_whitespace:
        blame_opts.append("-w")
    if args.detect_moves:
        # -M: lines moved within a file; -C: lines moved/copied from other files.
        blame_opts += ["-M", "-C"]

    credit = bot_credit_map(args.repo, args.rev) if args.credit_bots else {}
    totals: Counter = Counter()
    excluded = 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(blame_file, args.repo, args.rev, f, args.by, blame_opts,
                               args.exclude_author, credit): f
                   for f in files}
        for fut in futures:
            try:
                counts, n_excl = fut.result()
                totals.update(counts)
                excluded += n_excl
            except GitError as e:
                print(f"warning: skipping {futures[fut]}: {e}", file=sys.stderr)
            done += 1
            if args.progress and sys.stderr.isatty():
                print(f"\rblamed {done}/{len(files)} files", end="", file=sys.stderr)
    if args.progress and sys.stderr.isatty():
        print(file=sys.stderr)
    return totals, len(files), excluded


# --------------------------------------------------------------------------- #
# log mode
# --------------------------------------------------------------------------- #

def count_by_log(args) -> tuple[Counter, int, int]:
    """Sum lines added per author across commit history."""
    marker = "\x1e"  # record separator: precedes each commit's author line
    cmd = ["log", args.rev, "--numstat", "--no-renames",
           f"--format={marker}%H%x00%aN%x00%aE"]
    if not args.include_merges:
        cmd.append("--no-merges")
    if args.since:
        cmd.append(f"--since={args.since}")
    if args.until:
        cmd.append(f"--until={args.until}")
    if args.ignore_whitespace:
        cmd.append("-w")

    credit = bot_credit_map(args.repo, args.rev) if args.credit_bots else {}
    out = run_git(args.repo, *cmd).decode("utf-8", errors="replace")
    totals: Counter = Counter()
    files_seen = set()
    excluded = 0
    key = None
    skip_author = False
    # Note: split on "\n" only; str.splitlines() would also split on the \x1e marker.
    for line in out.split("\n"):
        if line.startswith(marker):
            commit, name, email = line[1:].split("\0", 2)
            name, email = credit.get(commit, (name, email))
            key = author_key(name, email, args.by)
            skip_author = author_excluded(name, email, args.exclude_author)
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3 or key is None:
            continue
        added, _deleted, path = parts
        if added == "-" or not path_selected(path, args.include, args.exclude):
            continue  # binary or filtered out
        files_seen.add(path)
        if skip_author:
            excluded += int(added)
        else:
            totals[key] += int(added)
    return totals, len(files_seen), excluded


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def report(totals: Counter, nfiles: int, excluded: int, args) -> None:
    grand = sum(totals.values())
    rows = [(author, n, 100.0 * n / grand if grand else 0.0)
            for author, n in totals.most_common()]
    if args.min_percent:
        rows = [r for r in rows if r[2] >= args.min_percent]

    if args.format == "json":
        json.dump({
            "mode": args.mode,
            "revision": args.rev,
            "files": nfiles,
            "total_lines": grand,
            "excluded_author_lines": excluded,
            "excluded_authors": args.exclude_author,
            "authors": [{"author": a, "lines": n, "percent": round(p, 2)}
                        for a, n, p in rows],
        }, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return

    if args.format == "csv":
        w = csv.writer(sys.stdout)
        w.writerow(["author", "lines", "percent"])
        for a, n, p in rows:
            w.writerow([a, n, f"{p:.2f}"])
        return

    what = "lines at " + args.rev if args.mode == "blame" else "lines added in history"
    print(f"Repository: {os.path.abspath(args.repo)}")
    print(f"Counted {grand:,} {what} across {nfiles:,} files")
    if args.exclude_author:
        print(f"Excluded {excluded:,} lines by authors matching: "
              f"{', '.join(args.exclude_author)}")
    print()
    if not rows:
        print("No lines found.")
        return
    width = max(len("Author"), *(len(a) for a, _, _ in rows))
    bar_width = 30
    print(f"{'Author':<{width}}  {'Lines':>10}  {'Percent':>7}")
    print(f"{'-' * width}  {'-' * 10}  {'-' * 7}")
    for a, n, p in rows:
        bar = "#" * max(1 if n else 0, round(p / 100 * bar_width))
        print(f"{a:<{width}}  {n:>10,}  {p:>6.2f}%  {bar}")


# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Show the percentage of lines of code written by each "
                    "contributor in a git repository.",
        epilog="Author identities are normalized through the repo's .mailmap if present.",
    )
    p.add_argument("repo", nargs="?", default=".", help="path to the git repository (default: .)")
    p.add_argument("--mode", choices=["blame", "log"], default="blame",
                   help="blame: credit lines that exist at REV (default); "
                        "log: sum lines added over history")
    p.add_argument("--rev", default="HEAD", help="revision to analyze (default: HEAD)")
    p.add_argument("--by", choices=["both", "name", "email"], default="both",
                   help="how to identify contributors (default: name and email)")
    p.add_argument("--include", action="append", default=[], metavar="GLOB",
                   help="only count files matching GLOB (repeatable), e.g. '*.py'")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="skip files matching GLOB (repeatable), e.g. 'vendor/*'")
    p.add_argument("--exclude-author", action="append", default=[], metavar="GLOB",
                   help="leave out lines by authors whose name or email matches GLOB "
                        "(case-insensitive, repeatable); percentages are computed "
                        "over the remaining authors")
    p.add_argument("--mailmap", metavar="FILE", default=DEFAULT_MAILMAP,
                   help="extra .mailmap-format file that merges contributor identities, "
                        "applied on top of the repo's own .mailmap "
                        "(default: authors.mailmap next to this script, if it exists)")
    p.add_argument("--no-mailmap", dest="mailmap", action="store_const", const=None,
                   help="don't apply the extra mailmap file")
    p.add_argument("--credit-bots", action="store_true",
                   help="credit lines from bot-authored commits (e.g. Copilot coding "
                        "agent) to the human named in the commit's Co-authored-by trailer")
    p.add_argument("-w", "--ignore-whitespace", action="store_true",
                   help="ignore whitespace-only changes when attributing lines")
    p.add_argument("--detect-moves", action="store_true",
                   help="(blame) credit moved/copied lines to their original author; slower")
    p.add_argument("--since", help="(log) only commits after this date")
    p.add_argument("--until", help="(log) only commits before this date")
    p.add_argument("--include-merges", action="store_true",
                   help="(log) also count lines from merge commits")
    p.add_argument("--min-percent", type=float, default=0.0,
                   help="hide contributors below this percentage")
    p.add_argument("--format", choices=["table", "csv", "json"], default="table")
    p.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4,
                   help="parallel git blame processes (default: CPU count)")
    p.add_argument("--no-progress", dest="progress", action="store_false",
                   help="don't show a progress counter")
    return p.parse_args(argv)


def configure_mailmap(args) -> None:
    """Make every later git command apply args.mailmap (if set and present)."""
    GIT_CONFIG.clear()
    if not args.mailmap:
        return
    if os.path.isfile(args.mailmap):
        GIT_CONFIG.extend(["-c", f"mailmap.file={os.path.abspath(args.mailmap)}"])
    elif args.mailmap != DEFAULT_MAILMAP:
        raise GitError(f"mailmap file not found: {args.mailmap}")


def count_commits(args) -> tuple[Counter, int]:
    """Count non-merge commits per author up to args.rev, honoring --credit-bots,
    --exclude-author, --since and --until. Returns (counts, excluded commits)."""
    cmd = ["log", args.rev, "--no-merges", "--format=%H%x00%aN%x00%aE"]
    if args.since:
        cmd.append(f"--since={args.since}")
    if args.until:
        cmd.append(f"--until={args.until}")
    credit = bot_credit_map(args.repo, args.rev) if args.credit_bots else {}
    counts: Counter = Counter()
    excluded = 0
    for line in run_git(args.repo, *cmd).decode("utf-8", errors="replace").split("\n"):
        if not line:
            continue
        commit, name, email = line.split("\0", 2)
        name, email = credit.get(commit, (name, email))
        if author_excluded(name, email, args.exclude_author):
            excluded += 1
        else:
            counts[author_key(name, email, args.by)] += 1
    return counts, excluded


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        configure_mailmap(args)
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        run_git(args.repo, "rev-parse", "--verify", "--quiet", args.rev + "^{commit}")
    except GitError:
        print(f"error: {args.repo!r} is not a git repository or {args.rev!r} "
              f"is not a valid revision", file=sys.stderr)
        return 1
    try:
        if args.mode == "blame":
            totals, nfiles, excluded = count_by_blame(args)
        else:
            totals, nfiles, excluded = count_by_log(args)
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    report(totals, nfiles, excluded, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
