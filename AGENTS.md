# AGENTS.md

This folder contains `git_authorship.py`, a script that reports what percentage
of a git repository's lines each contributor wrote. It is used to measure
student contributions to class project repositories that are all built from the
same instructor-provided base code.

## Always exclude the instructor's contributions

Susan Hammond (`sahammond@lipscomb.edu`) is the instructor and wrote the base
code. **Every line count must exclude her contributions**, so that percentages
reflect only the students' work. Do this on every run, in every mode, even if
the request doesn't mention it.

Pass `--exclude-author sahammond@lipscomb.edu` on every run. `authors.mailmap`
maps her GitHub account (`sah0017`) to that email, so the one exclusion also
covers base-code lines written by AI agents she ran. `--exclude-author sah0017`
is kept as a safeguard in case the mailmap isn't applied.

If a repository shows her under another name or email, add it to the
instructor section of `authors.mailmap`.

Don't report her line count or percentage as part of the results. The script
prints how many lines it excluded; that number can be mentioned only to show
the exclusion was applied.

## Credit AI-agent work to the student who requested it

Students may use Copilot or Claude to help write their projects. That work
belongs to the student, so always pass `--credit-bots`:

- Commits made from the student's own machine with Claude Code or Copilot in
  the editor already list the student as author. No action is needed.
- Commits made by a cloud agent (for example `copilot-swe-agent[bot]` or
  `claude[bot]`) list the bot as author and the requesting student in a
  `Co-authored-by:` line. `--credit-bots` gives those lines to the student.

If a bot still appears in the results, its commits named no person as a
co-author. Report those lines separately as unattributed, and don't guess
which student made the request.

## Standard commands

```bash
# Current code: who wrote the lines that exist now
python3 git_authorship.py <repo> --no-progress --credit-bots \
    --exclude-author sahammond@lipscomb.edu --exclude-author sah0017

# Full history: all lines each student ever added
python3 git_authorship.py <repo> --mode log --credit-bots \
    --exclude-author sahammond@lipscomb.edu --exclude-author sah0017 \
    --exclude 'lib/*' --exclude 'bin/*' --exclude 'pyvenv.cfg'
```

## Known issues in student repositories

- **Committed virtual environment.** The base code's history includes a Python
  virtual environment under `lib/` and `bin/` (about 627,000 lines) that was
  committed and later deleted. Always exclude `lib/*`, `bin/*` and
  `pyvenv.cfg` in `--mode log`, or the results are meaningless.
- **Duplicate files.** Students sometimes commit the same SQL under two
  filenames, which counts their work twice. Check for files with identical
  content and report the de-duplicated count alongside the raw count.
- **Empty commits.** A commit can add an empty file whose contents another
  student writes later. That student is credited with the lines. Mention it
  when a commit message claims work that the line counts don't show.
- **Several identities per student.** A student may commit under different
  names or emails, for example a GitHub username for web uploads and their
  real name locally. This is especially common with `--credit-bots`: a cloud
  agent names the student by GitHub username and a
  `...@users.noreply.github.com` email, which won't match the name and school
  email on the student's other commits. See "Merging identities" below.

## Merging identities (`authors.mailmap`)

`git_authorship.py` automatically applies `authors.mailmap` from this folder to
every repository, on top of the repository's own `.mailmap`. The file is in
standard git mailmap format and contains instructions for adding entries.

- Before reporting, list each repository's identities:
  `git -C <repo> log --format='%aN <%aE>' | sort | uniq -c` and
  `git -C <repo> log --format='%(trailers:key=Co-authored-by,valueonly)' | sort -u`.
- When two identities are clearly the same student (same school email, or a
  GitHub username that obviously matches their name), add a line to the
  students section of `authors.mailmap` and rerun. Map to the student's
  school email.
- When it isn't clear, don't add an entry. Report both identities and point
  out the possible match so the instructor can decide.
- Never commit a `.mailmap` or anything else into a student repository. The
  exceptions are `commitStats.md`, written by `update_commit_stats.py` (see
  below), and merging updated base code when the instructor asks for it.

## Merging base-code updates into student repositories

The base code is `/Users/sahammond/Database/db-app-example`
(GitHub: `sah0017/db-app-example`). All student repositories share its history,
so an update is merged in, not copied. Only do this when the instructor asks.

For each repository: confirm it has no uncommitted changes, pull with
`--ff-only`, fetch the base branch, merge with `--no-ff`, and run `pytest`.
Push only if the tests pass. If the merge conflicts, run `git merge --abort`
and report the repository instead of resolving the conflict in the students'
files. Merge commits are made as the instructor, so they don't change the
students' numbers.

## Updating commitStats.md in every repository

`update_commit_stats.py` runs the standard analysis on every repository in
`/Users/sahammond/Fall26DB`. For each one it writes `commitStats.md` with the
time of the run and each student's commits, lines and percentages, then
commits only that file and pushes it. It already applies the instructor
exclusion, `--credit-bots`, the virtual-environment excludes and the mailmap.

```bash
python3 update_commit_stats.py --dry-run        # preview; changes nothing
python3 update_commit_stats.py                  # pull, write, commit, push
python3 update_commit_stats.py --repo Fall26RecipeMgmt   # one repo only
```

- Pushing changes the students' GitHub repositories. Run without `--dry-run`
  only when the instructor asks for an update.
- A repository is skipped, not modified, if it has uncommitted changes, isn't
  on a branch with an upstream, or can't be fast-forwarded. Report skipped
  repositories and their reasons; don't force, stash or reset anything.
- Commits are made as the instructor, so they're excluded from future counts
  automatically.

`commitStats.md` has two tables:

- **Students:** commits; spec lines (files in `specs/`) and code lines (all
  other files) in the current code; lines added over the history; and pull
  requests opened, merged and reviewed. The pull request columns come from
  GitHub through the `gh` CLI, which must be logged in (`gh auth status`).
  `--no-github` skips them.
- **Features:** one row per `specs/features/*.md`, read from the spec's header
  (`feature`, `owner`, `status`, `paths`). It compares the owner with who wrote
  the spec and the files in `paths`, and flags problems in the Check column.
  Specs owned by the instructor, such as the `01-actors` example, are skipped.
  When reporting, call out every flagged feature.
