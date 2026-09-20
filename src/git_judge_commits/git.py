"""Reading commits out of a repository, and shrinking them to fit a prompt."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

RECORD = "\x1e"
UNIT = "\x1f"

_LOG_FORMAT = f"{RECORD}%H{UNIT}%an{UNIT}%aI{UNIT}%P{UNIT}%B"


class GitError(RuntimeError):
    """git exited non-zero."""


def run_git(repo: str, *args: str, timeout: float | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out after {timeout:.0f}s") from None
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


_git = run_git


@dataclass
class FileChange:
    path: str
    insertions: int | None
    deletions: int | None

    def as_state(self) -> dict[str, object]:
        return {
            "path": self.path,
            "insertions": "binary" if self.insertions is None else self.insertions,
            "deletions": "binary" if self.deletions is None else self.deletions,
        }


@dataclass
class Commit:
    sha: str
    author: str
    date: str
    parents: list[str]
    message: str
    files: list[FileChange] = field(default_factory=list)
    diff: str = ""
    diff_truncated: bool = False

    @property
    def short(self) -> str:
        return self.sha[:8]

    @property
    def subject(self) -> str:
        return self.message.strip().splitlines()[0] if self.message.strip() else ""

    def as_state(self) -> dict[str, object]:
        """The state Jev judges: what the author said, and what they actually changed."""
        return {
            "commit_message": self.message.strip(),
            "author": self.author,
            "date": self.date,
            "is_merge": len(self.parents) > 1,
            "files_changed": [f.as_state() for f in self.files],
            "diff_truncated": self.diff_truncated,
            "diff": self.diff,
        }


def _verify(repo: str, ref: str) -> None:
    """Fail on an unknown ref here, rather than letting git call it a path."""
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except GitError:
        raise GitError(f"unknown revision {ref!r}") from None


def _require_repo(repo: str) -> None:
    try:
        _git(repo, "rev-parse", "--git-dir")
    except GitError:
        raise GitError(f"not a git repository: {repo}") from None


def _require_head(repo: str) -> None:
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    except GitError:
        raise GitError("repository has no commits yet") from None


@dataclass
class Selection:
    """Which commits to judge, in which repository, and how we decided."""

    repo: str
    selector: list[str]
    description: str


# Only consulted when origin/HEAD is unset, which is common in repos cloned
# before the remote grew a default or set up with `git init`.
FALLBACK_DEFAULTS = ("origin/main", "origin/master", "main", "master")


def detect_default_branch(repo: str) -> str | None:
    """The branch a merge request would target.

    `origin/HEAD` is the only source that actually knows -- guessing main or
    master gets repos whose default is something else (dev, trunk) wrong.
    """
    try:
        if head := _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip():
            return head
    except GitError:
        pass
    for candidate in FALLBACK_DEFAULTS:
        try:
            _git(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
            return candidate
        except GitError:
            continue
    return None


def current_branch(repo: str) -> str | None:
    """The checked-out branch, or None when HEAD is detached."""
    name = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return None if name == "HEAD" else name


def _same_branch(current: str | None, default: str) -> bool:
    return current is not None and current in (default, default.removeprefix("origin/"))


def _count(repo: str, spec: str) -> int:
    return int(_git(repo, "rev-list", "--count", spec).strip() or 0)


def plan(repo: str, spec: str | None, limit: int, base: str | None = None) -> Selection:
    """Decide which commits to judge, in the repository we were pointed at.

    An explicit revision range wins; with none given, a feature branch judges
    what it adds on top of the default branch -- the whole merge request --
    because that is the unit someone reviews. Anywhere else, the last `limit`
    commits. Pull request URLs are resolved before this, in the CLI.
    """
    _require_repo(repo)

    if spec is not None:
        separator = "..." if "..." in spec else ".." if ".." in spec else None
        if separator is None:
            _verify(repo, spec)
            _require_head(repo)
            return Selection(repo, [f"{spec}..HEAD"], f"{spec}..HEAD")
        left, _, right = spec.partition(separator)
        for endpoint in (left or "HEAD", right or "HEAD"):
            _verify(repo, endpoint)
        return Selection(repo, [spec], spec)

    _require_head(repo)
    fallback = Selection(repo, ["HEAD", f"-n{limit}"], f"last {limit} commits of HEAD")

    if base is not None:
        _verify(repo, base)
        default = base
    elif (found := detect_default_branch(repo)) is not None:
        default = found
    else:
        return fallback

    current = current_branch(repo)
    if base is None and _same_branch(current, default):
        return fallback  # on the default branch, there is no branch to judge
    if not _count(repo, f"{default}..HEAD"):
        return fallback  # nothing on top of it, so fall back rather than judge nothing

    return Selection(repo, [f"{default}..HEAD"], f"{default}..HEAD ({current or 'detached HEAD'})")


def read_commits(
    repo: str,
    selection: Selection,
    *,
    limit: int,
    include_merges: bool,
    exclude: list[str],
    diff_budget: int,
) -> list[Commit]:
    args = ["log", f"--format={_LOG_FORMAT}", *selection.selector]
    if not include_merges:
        args.append("--no-merges")
    if limit and f"-n{limit}" not in selection.selector:
        args.append(f"-n{limit}")

    commits: list[Commit] = []
    for record in _git(repo, *args).split(RECORD):
        if not record.strip():
            continue
        sha, author, date, parents, message = record.split(UNIT, 4)
        commits.append(
            Commit(
                sha=sha,
                author=author,
                date=date,
                parents=parents.split(),
                message=message,
            )
        )

    pathspec = ["--", *(f":(exclude){p}" for p in exclude)] if exclude else []
    for commit in commits:
        commit.files = _read_numstat(repo, commit.sha, pathspec)
        raw = _git(
            repo,
            "show",
            commit.sha,
            "--format=",
            "--no-color",
            "--find-renames",
            "--unified=3",
            *pathspec,
        )
        commit.diff, commit.diff_truncated = budget_diff(raw, diff_budget)
    return commits


def _read_numstat(repo: str, sha: str, pathspec: list[str]) -> list[FileChange]:
    out = _git(repo, "show", sha, "--format=", "--numstat", "--find-renames", *pathspec)
    files: list[FileChange] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        ins, dels, path = parts
        files.append(
            FileChange(
                path=path,
                insertions=None if ins == "-" else int(ins),
                deletions=None if dels == "-" else int(dels),
            )
        )
    return files


def budget_diff(diff: str, budget: int) -> tuple[str, bool]:
    """Fit a diff into `budget` characters, spending it evenly across files.

    Truncating the tail would hide every file after the first, which is exactly
    where a mismatched commit message tends to hide its extra change.
    """
    if budget <= 0 or len(diff) <= budget:
        return diff, False

    chunks: list[list[str]] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") or not chunks:
            chunks.append([])
        chunks[-1].append(line)

    per_file = max(budget // len(chunks), 200)
    out: list[str] = []
    truncated = False
    for chunk in chunks:
        text = "".join(chunk)
        if len(text) > per_file:
            text = text[:per_file] + f"\n... [{len(text) - per_file} chars truncated]\n"
            truncated = True
        out.append(text)
    return "".join(out), truncated
