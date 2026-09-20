"""The typed questions Jev answers about a single commit.

Jev is a System One model: every field below becomes one independent question
asked against the same state, so each description has to stand on its own, and
each has to be the kind of judgement an expert makes in a glance. Anything that
bundles several factors comes back with no margin -- which is why compatibility
is a five-way choice rather than one `breaking: bool`.
"""

from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import UseEnumMemberDocstrings

# Framing for the state, not the questions themselves: Jev takes the prompt as
# only the thing being judged, so anything asked belongs on the fields below.
INSTRUCTIONS = """\
You are judging a single git commit. The state is a JSON object holding the commit
message the author wrote, the list of files it touched, and its unified diff. The diff
is the ground truth about what changed; the message is a claim about it that may or may
not hold. If `diff_truncated` is true, parts of the diff were cut for length, so judge
on what is present and on the file list.
"""

CommitType = Literal[
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
]


class Compat(UseEnumMemberDocstrings, StrEnum):
    """What kind of compatibility this commit puts at stake."""

    none = "none"
    """Nothing observable changes: documentation, tests, comments, tooling, formatting."""

    internal = "internal"
    """Implementation moved around, but every caller sees exactly what it saw before."""

    behaviour = "behaviour"
    """Same interface, different runtime outcome: input now rejected that was accepted,
    a side effect added or removed, a different result for the same call. Nobody has to
    change their code, but they will notice."""

    interface = "interface"
    """Callers must change their code: a function, endpoint, flag, config key or schema
    was removed, renamed, or given a different signature, type or default."""

    data = "data"
    """Persisted or transmitted data changes shape, so a deploy needs a migration or a
    coordinated rollout: database schema, stored format, wire protocol, token contents."""


BREAKING = frozenset({Compat.interface, Compat.data})


class Attention(UseEnumMemberDocstrings, IntEnum):
    """How closely a reviewer should look at this commit."""

    none = 0
    """Mechanical, generated or trivial: formatting, version bumps, lockfiles, typos."""

    low = 1
    """Small self-contained change with obvious intent and no wider consequences."""

    moderate = 2
    """Ordinary feature or bugfix work a reviewer should read but need not agonise over."""

    elevated = 3
    """Touches shared behaviour, public interfaces or non-obvious logic; reward careful reading."""

    high = 4
    """Risky: concurrency, auth, data migrations, error handling, or wide blast radius."""

    critical = 5
    """Demands scrutiny before shipping: security-sensitive, destructive, or silently breaking."""


class CommitClassification(BaseModel):
    """Classify one git commit from its message and diff."""

    compat: Compat = Field(
        description=(
            "What does this commit put at stake for people already depending on this "
            "code? Judge the strongest claim the diff supports, and judge the code as "
            "it is used, not the words of the message."
        )
    )

    worth_attention: Attention = Field(
        description=(
            "How much reviewer attention does this change deserve, judged on the risk "
            "and subtlety of the code it touches rather than on how many lines it moves?"
        )
    )

    type: CommitType = Field(
        description=(
            "Which Conventional Commits type describes what this diff actually does? "
            "feat adds user-visible capability; fix repairs broken behaviour; docs is "
            "documentation only; style is formatting with no behaviour change; refactor "
            "restructures without changing behaviour; perf improves speed or resource "
            "use; test touches tests only; build changes the build system or "
            "dependencies; ci changes pipeline configuration; chore is routine "
            "housekeeping; revert undoes an earlier commit."
        )
    )

    single_concern: bool = Field(
        description=(
            "Does this commit do exactly one thing? Answer yes when every hunk serves "
            "one purpose a reviewer could accept or reject as a unit, counting "
            "supporting tests, docs and generated files as part of that one purpose. "
            "Answer no when unrelated work rides along, such as a feature plus a "
            "drive-by refactor, or two independent fixes landed together."
        )
    )

    message_matching: bool = Field(
        description=(
            "Is the commit message a truthful and adequate description of this diff? "
            "Answer no if the message claims something the diff does not do, omits a "
            "significant unrelated change smuggled in alongside it, or is so vague "
            "('fix stuff', 'wip', 'update') that it tells a reader nothing about the "
            "change."
        )
    )

    @property
    def breaking(self) -> bool:
        """Derived in code, not asked: the model judges the kind, we apply the policy."""
        return self.compat in BREAKING
