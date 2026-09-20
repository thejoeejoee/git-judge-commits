"""Turning verdicts into an exit code, so a pipeline can refuse a branch.

A condition reads the way the table does -- `concern=mixed`, `compat=interface`,
`attention>=4` -- because the column names are what somebody has just been
looking at when they decide what to gate on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Sequence

from .model import Attention, CommitClassification, Compat

OPERATORS = ("!=", ">=", "<=", "=", ">", "<")
ALTERNATIVE = "|"

# A field the table shows, and how to read it off a classification. `order` is
# set only where the values form a scale, which is what >= and <= need.
@dataclass(frozen=True)
class Field:
    question: str
    read: Callable[[CommitClassification], str]
    values: tuple[str, ...]
    order: tuple[str, ...] | None = None
    aliases: dict[str, str] | None = None

    def normalise(self, value: str) -> str:
        value = value.strip().lower()
        value = (self.aliases or {}).get(value, value)
        if value not in self.values:
            allowed = ", ".join(self.values)
            raise ValueError(f"not a {self.question} value: {value!r} (expected one of: {allowed})")
        return value


_COMPAT = tuple(c.value for c in Compat)
_ATTENTION = tuple(str(int(a)) for a in Attention)

FIELDS: dict[str, Field] = {
    "compat": Field(
        question="compat",
        read=lambda c: c.compat.value,
        values=_COMPAT,
        order=_COMPAT,  # declared least to most disruptive, so >= means "or worse"
    ),
    "attention": Field(
        question="worth_attention",
        read=lambda c: str(int(c.worth_attention)),
        values=_ATTENTION,
        order=_ATTENTION,
        aliases={a.name: str(int(a)) for a in Attention},
    ),
    "type": Field(
        question="type",
        read=lambda c: c.type,
        values=(
            "feat", "fix", "docs", "style", "refactor", "perf",
            "test", "build", "ci", "chore", "revert",
        ),
    ),
    "concern": Field(
        question="single_concern",
        read=lambda c: "single" if c.single_concern else "mixed",
        values=("single", "mixed"),
        aliases={"ok": "single", "one": "single", "multi": "mixed", "nok": "mixed"},
    ),
    "message": Field(
        question="message_matching",
        read=lambda c: "ok" if c.message_matching else "nok",
        values=("ok", "nok"),
        aliases={"match": "ok", "true": "ok", "yes": "ok",
                 "mismatch": "nok", "false": "nok", "no": "nok"},
    ),
    "breaking": Field(
        question="compat",  # derived from it, so it is that answer's confidence
        read=lambda c: "yes" if c.breaking else "no",
        values=("yes", "no"),
        aliases={"true": "yes", "false": "no"},
    ),
}

# Not a field: fails when any answer was too close to the threshold to report.
UNSURE = "unsure"


@dataclass(frozen=True)
class Condition:
    source: str
    field: str | None  # None for the `unsure` pseudo-condition
    operator: str = "="
    values: tuple[str, ...] = ()

    def matches(self, verdict) -> bool:
        if self.field is None:
            return bool(verdict.low_confidence)
        classification = verdict.classification
        if classification is None:
            return False
        spec = FIELDS[self.field]
        # A suppressed answer is not an answer; gating on it would fail a branch
        # over a coin flip.
        if verdict.unsure(spec.question):
            return False

        actual = spec.read(classification)
        if self.operator in ("=", "!="):
            hit = actual in self.values
            return hit if self.operator == "=" else not hit

        if spec.order is None:
            raise ValueError(f"{self.field} has no order, so {self.operator} means nothing here")
        rank = spec.order.index(actual)
        threshold = min(spec.order.index(v) for v in self.values)
        return {
            ">=": rank >= threshold,
            ">": rank > threshold,
            "<=": rank <= threshold,
            "<": rank < threshold,
        }[self.operator]


def parse(expressions: Sequence[str]) -> list[Condition]:
    """Parse `--fail-on` values: comma-separated `field=value`, `|` for alternatives."""
    conditions: list[Condition] = []
    for expression in expressions:
        for clause in expression.split(","):
            if clause := clause.strip():
                conditions.append(_parse_clause(clause))
    return conditions


def _parse_clause(clause: str) -> Condition:
    lowered = clause.lower()
    if lowered == UNSURE:
        return Condition(source=clause, field=None)

    match = re.match(rf"^([a-z_]+)\s*({'|'.join(re.escape(o) for o in OPERATORS)})\s*(.+)$", lowered)
    if not match:
        # a bare field name, for the ones where there is only one interesting answer
        if lowered in ("breaking", "mixed", "mismatch"):
            shorthand = {"breaking": "breaking=yes", "mixed": "concern=mixed", "mismatch": "message=nok"}
            expanded = _parse_clause(shorthand[lowered])
            # report it back the way it was written, not the way it expands
            return Condition(clause, expanded.field, expanded.operator, expanded.values)
        raise ValueError(
            f"cannot read {clause!r}: expected something like compat=interface, "
            f"attention>=4, concern=mixed, message=nok, breaking, or unsure"
        )

    name, operator, raw = match.groups()
    name = {"single_concern": "concern", "message_matching": "message", "worth_attention": "attention"}.get(name, name)
    if name not in FIELDS:
        raise ValueError(f"not a field: {name!r} (expected one of: {', '.join(FIELDS)}, or {UNSURE})")

    spec = FIELDS[name]
    values = tuple(spec.normalise(part) for part in raw.split(ALTERNATIVE) if part.strip())
    if not values:
        raise ValueError(f"no value given in {clause!r}")
    if operator not in ("=", "!=") and spec.order is None:
        raise ValueError(f"{name} values have no order, so only = and != work (got {operator!r})")
    return Condition(source=clause, field=name, operator=operator, values=values)


def evaluate(conditions: Sequence[Condition], verdicts: Sequence) -> list[tuple[Condition, list]]:
    """Which conditions tripped, and on which commits."""
    tripped = []
    for condition in conditions:
        if hits := [v for v in verdicts if condition.matches(v)]:
            tripped.append((condition, hits))
    return tripped
