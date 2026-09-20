"""A disk cache for verdicts, so re-running a range costs nothing.

A commit is immutable and Jev is deterministic enough for the same state to get
the same answer, so a verdict only goes stale when something about the *question*
changes. The key therefore covers the state sent, the model asked, the boolean
threshold, and a fingerprint of the question definitions themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pydantic_ai

from . import model as model_module

CACHE_VERSION = 1

# `jev-latest` and `jev-preview` move when TypeSafe ship a release, which shifts
# answers under a key that cannot see the change. Pinned versions never expire.
MOVING_ALIASES = ("latest", "preview")
MOVING_TTL_SECONDS = 7 * 24 * 60 * 60


def default_cache_dir() -> Path:
    if override := os.environ.get("GIT_JUDGE_COMMITS_CACHE"):
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "git-judge-commits"


def _own_version() -> str:
    try:
        return version("git-judge-commits")
    except PackageNotFoundError:
        return "unknown"


def question_fingerprint() -> str:
    """Hash everything that shapes the request, so a change invalidates its answers.

    Three sources, because no one of them is enough:

    * `model.py` verbatim -- the JSON schema omits enum member docstrings, and
      those carry the whole rubric. Hashing the source also catches edits made
      between releases, which a version number never would.
    * our own version, for changes elsewhere that alter how the request is built.
    * the pydantic-ai version, which owns how these questions reach Jev at all.

    The state sent, the model and the threshold need no fingerprint: they are
    already in the key verbatim.
    """
    try:
        source = Path(model_module.__file__ or "").read_bytes()
    except OSError:
        # Not every install shape keeps the source readable. Fall back to the
        # schema, which still moves when a field or option changes -- only the
        # enum docstrings go unwatched, and those cannot change without a release.
        source = json.dumps(model_module.CommitClassification.model_json_schema()).encode()
    material = source + f"\x00{_own_version()}\x00{pydantic_ai.__version__}".encode()
    return hashlib.sha256(material).hexdigest()[:16]


def _is_moving(model: str) -> bool:
    return any(alias in model for alias in MOVING_ALIASES)


@dataclass
class Cache:
    directory: Path
    fingerprint: str
    enabled: bool = True
    refresh: bool = False
    hits: int = 0
    writes: int = 0

    @classmethod
    def open(cls, directory: Path | None, *, enabled: bool = True, refresh: bool = False) -> Cache:
        return cls(
            directory=directory or default_cache_dir(),
            fingerprint=question_fingerprint(),
            enabled=enabled,
            refresh=refresh,
        )

    def key(self, *, sha: str, state: str, model: str, threshold: float) -> str:
        material = "\x00".join([str(CACHE_VERSION), self.fingerprint, sha, model, f"{threshold:.4f}", state])
        return hashlib.sha256(material.encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str, model: str) -> dict | None:
        if not self.enabled or self.refresh:
            return None
        try:
            entry = json.loads(self._path(key).read_text())
        except (OSError, ValueError):
            return None  # a missing or corrupt entry is simply a miss
        if entry.get("version") != CACHE_VERSION:
            return None
        if _is_moving(model) and time.time() - entry.get("created", 0) > MOVING_TTL_SECONDS:
            return None
        self.hits += 1
        return entry

    def put(self, key: str, entry: dict) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(entry | {"version": CACHE_VERSION, "created": time.time()}))
            os.replace(temporary, path)  # atomic, so a killed run leaves no half-entry
            self.writes += 1
        except OSError:
            pass  # a cache that cannot be written is not a reason to fail the run
