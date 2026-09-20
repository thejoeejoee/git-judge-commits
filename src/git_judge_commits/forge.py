"""Judging a pull or merge request straight from its URL.

Every forge that matters publishes the head of a pull request as an ordinary git
ref, so a URL needs no forge-specific diff handling at all: fetch the ref, work
out the base, and hand an ordinary revision range back to the normal pipeline.

A blobless shallow fetch makes that cheap even against a huge repository --
`git show` pulls the few blobs it needs on demand.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from .git import GitError, Selection, run_git

Forge = Literal["github", "gitlab"]
Report = Callable[[str], None]

FETCH_TIMEOUT = 180.0
API_TIMEOUT = 30.0

# Ref namespace we fetch into. Keeping our refs out of refs/heads and refs/remotes
# means judging a PR never disturbs a checkout the user cares about.
REF_NAMESPACE = "refs/git-judge-commits"

# GitLab puts `/-/` between the project path and the route, which is the only
# reliable place to split: groups nest arbitrarily deep.
_GITLAB = re.compile(r"^/(?P<project>.+?)/-/merge_requests/(?P<number>\d+)")
_GITLAB_LEGACY = re.compile(r"^/(?P<project>.+?)/merge_requests/(?P<number>\d+)")
# GitHub is a fixed two segments; Gitea and Forgejo copy it but spell it `pulls`.
_GITHUB = re.compile(r"^/(?P<project>[^/]+/[^/]+)/pulls?/(?P<number>\d+)")

# How far to reach for a merge base before giving up. A shallow fetch usually
# lands short of it, and each step costs a round trip, so widen fast.
DEEPEN_STEPS = (250, 1000, 5000)
INITIAL_DEPTH = 50


@dataclass(frozen=True)
class PullRequest:
    host: str
    project: str
    number: int
    forge: Forge

    @property
    def refspec(self) -> str:
        branch = "merge-requests" if self.forge == "gitlab" else "pull"
        return f"refs/{branch}/{self.number}/head"

    @property
    def clone_url(self) -> str:
        return f"https://{self.host}/{self.project}.git"

    @property
    def label(self) -> str:
        marker = "!" if self.forge == "gitlab" else "#"
        return f"{self.project}{marker}{self.number}"


def parse_url(text: str) -> PullRequest | None:
    """Recognise a pull or merge request URL, or return None for anything else."""
    if not re.match(r"^https?://", text):
        return None
    from urllib.parse import urlparse

    parsed = urlparse(text)
    if not parsed.netloc:
        return None
    path = parsed.path.rstrip("/")

    for pattern, forge in ((_GITLAB, "gitlab"), (_GITHUB, "github"), (_GITLAB_LEGACY, "gitlab")):
        if match := pattern.match(path):
            return PullRequest(
                host=parsed.netloc,
                project=match.group("project"),
                number=int(match.group("number")),
                forge=forge,  # type: ignore[arg-type]
            )
    return None


# --- locating a repository to work in ----------------------------------------


def _normalise_remote(url: str) -> tuple[str, str] | None:
    """Reduce a remote URL to (host, project) so ssh and https forms compare equal."""
    url = url.strip()
    if match := re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?$", url):
        return match.group(1), match.group(2)
    if match := re.match(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?$", url):
        return match.group(1), match.group(2)
    return None


def _matching_local_repo(pr: PullRequest, start: str) -> str | None:
    """Use the repo we are standing in when it is the one the URL points at.

    Its objects are already on disk, so fetching one ref is far cheaper than
    cloning, however partially.
    """
    try:
        remotes = run_git(start, "remote", "-v")
    except GitError:
        return None
    wanted = (pr.host.lower(), pr.project.lower())
    for line in remotes.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if (found := _normalise_remote(parts[1])) and (found[0].lower(), found[1].lower()) == wanted:
            return start
    return None


def _scratch_repo(pr: PullRequest, cache_dir: Path) -> str:
    """A reusable partial clone, kept next to the verdict cache."""
    path = cache_dir / "repos" / pr.host / pr.project
    if not (path / "HEAD").exists() and not (path / ".git").exists():
        path.mkdir(parents=True, exist_ok=True)
        run_git(str(path), "init", "-q")
        run_git(str(path), "remote", "add", "origin", pr.clone_url)
    return str(path)


# --- working out the range ---------------------------------------------------


def _fetch(repo: str, url: str, *refspecs: str, depth: int | None = None, deepen: int | None = None) -> None:
    args = ["fetch", "-q", "--filter=blob:none"]
    if depth is not None:
        args.append(f"--depth={depth}")
    if deepen is not None:
        args.append(f"--deepen={deepen}")
    run_git(repo, *args, url, *refspecs, timeout=FETCH_TIMEOUT)


def _base_from_api(pr: PullRequest, url: str, repo: str) -> str | None:
    """Ask the forge CLI for the base commit, if one is installed and authorised.

    This is the answer the forge itself would give, so prefer it. GitLab refuses
    the API to anonymous callers even for public projects, so it often fails
    where the plain git route below still works.
    """
    if pr.forge == "github" and shutil.which("gh"):
        command = ["gh", "pr", "view", url, "--json", "baseRefOid", "-q", ".baseRefOid"]
    elif pr.forge == "gitlab" and shutil.which("glab"):
        command = ["glab", "mr", "view", str(pr.number), "--repo", f"{pr.host}/{pr.project}", "-F", "json"]
    else:
        return None

    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=API_TIMEOUT, cwd=repo)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None

    output = proc.stdout.strip()
    if pr.forge == "github":
        return output or None
    try:  # glab prints the whole merge request; the base sha lives under diff_refs
        return (json.loads(output).get("diff_refs") or {}).get("base_sha") or None
    except ValueError:
        return None


def _base_from_merge_base(repo: str, pr: PullRequest, head: str, report: Report) -> str:
    """Derive the base by fetching the default branch and meeting in the middle.

    Needs no API and no credentials, so it is what carries self-hosted GitLab and
    any public project. The cost is that a shallow history often has no common
    ancestor yet, and each widening is another round trip.
    """
    _fetch(repo, pr.clone_url, "HEAD", depth=INITIAL_DEPTH)
    default = run_git(repo, "rev-parse", "FETCH_HEAD").strip()

    for deepen in (None, *DEEPEN_STEPS):
        if deepen is not None:
            report(f"widening history (+{deepen}) to find the branch point")
            _fetch(repo, pr.clone_url, pr.refspec, "HEAD", deepen=deepen)
        try:
            return run_git(repo, "merge-base", head, default).strip()
        except GitError:
            continue

    raise GitError(
        f"cannot find where {pr.label} branched from the default branch; "
        "pass --base with a revision to compare against"
    )


def resolve(
    pr: PullRequest,
    url: str,
    *,
    start_repo: str,
    cache_dir: Path,
    base: str | None,
    report: Report,
) -> Selection:
    """Turn a pull request URL into an ordinary range in a usable repository."""
    if local := _matching_local_repo(pr, start_repo):
        repo, where = local, "this repository"
    else:
        repo, where = _scratch_repo(pr, cache_dir), "a partial clone"

    report(f"fetching {pr.label} into {where}")
    head_ref = f"{REF_NAMESPACE}/{pr.number}"
    _fetch(repo, pr.clone_url, f"+{pr.refspec}:{head_ref}", depth=INITIAL_DEPTH)
    head = run_git(repo, "rev-parse", head_ref).strip()

    if base is not None:
        resolved, how = run_git(repo, "rev-parse", f"{base}^{{commit}}").strip(), "--base"
    elif found := _base_from_api(pr, url, repo):
        resolved, how = found, "forge"
        # the base commit is usually outside a shallow fetch of the branch alone
        if not _has_commit(repo, resolved):
            _fetch(repo, pr.clone_url, resolved, deepen=DEEPEN_STEPS[0])
    else:
        resolved, how = _base_from_merge_base(repo, pr, head, report), "merge-base"

    return Selection(
        repo=repo,
        selector=[f"{resolved}..{head}"],
        description=f"{pr.host}/{pr.label} · base from {how}",
    )


def _has_commit(repo: str, sha: str) -> bool:
    try:
        run_git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        return True
    except GitError:
        return False
