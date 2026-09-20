"""Judge every commit in a range with Jev, and say whether the message tells the truth."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from dotenv import load_dotenv
from pydantic import ValidationError
from pydantic_ai import Agent
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text

from .cache import Cache
from . import forge, gate
from .git import Commit, GitError, plan, read_commits
from .model import INSTRUCTIONS, Attention, CommitClassification, Compat

DEFAULT_LIMIT = 32
DEFAULT_DIFF_BUDGET = 16_000
DEFAULT_CONCURRENCY = 8
DEFAULT_MIN_CONFIDENCE = 0.2

# Exit codes. A gate that tripped and a commit that could not be judged are
# different problems, and a pipeline may well want to treat them differently.
EXIT_GATE = 1
EXIT_ERROR = 2
EXIT_UNJUDGED = 3

QUESTIONS = ("compat", "worth_attention", "type", "single_concern", "message_matching")


COMPAT_STYLE = {
    Compat.none: "dim",
    Compat.internal: "dim",
    Compat.behaviour: "yellow",
    Compat.interface: "bold red",
    Compat.data: "bold red",
}
ATTENTION_STYLE = {0: "dim", 1: "dim", 2: "", 3: "yellow", 4: "red", 5: "bold red"}


@dataclasses.dataclass
class Verdict:
    commit: Commit
    classification: CommitClassification | None
    confidence: dict[str, float]
    scores: dict[str, float]
    error: str | None = None
    min_confidence: float = 0.0
    probabilities: dict[str, dict[str, float]] = dataclasses.field(default_factory=dict)
    model_name: str | None = None
    elapsed: float = 0.0
    input_tokens: int = 0
    cached: bool = False

    def unsure(self, field: str) -> bool:
        """Is this answer too close to the decision threshold to report?

        Confidence is a margin, not a probability of being right, so a low value
        means the question had no clear answer for this commit -- printing the
        answer anyway is the most misleading thing the tool can do.
        """
        conf = self.confidence.get(field)
        return conf is not None and conf < self.min_confidence

    @property
    def weakest(self) -> float | None:
        """Weakest margin among the answers actually reported.

        Suppressed answers are excluded: showing their margin here would
        understate how much the visible row can be trusted.
        """
        shown = [c for f, c in self.confidence.items() if not self.unsure(f)]
        return min(shown) if shown else None

    @property
    def low_confidence(self) -> list[str]:
        return [f for f in QUESTIONS if self.unsure(f)]


# --- classification ---------------------------------------------------------


def _from_entry(commit: Commit, entry: dict, floor: float) -> Verdict | None:
    """Rebuild a verdict from a cache entry, treating any surprise as a miss."""
    try:
        classification = CommitClassification.model_validate(entry["output"])
    except (KeyError, ValidationError):
        return None
    return Verdict(
        commit=commit,
        classification=classification,
        confidence=entry.get("confidence", {}),
        scores=entry.get("scores", {}),
        min_confidence=floor,
        probabilities=entry.get("probabilities", {}),
        model_name=entry.get("resolved_model"),
        elapsed=entry.get("elapsed", 0.0),
        input_tokens=entry.get("input_tokens", 0),
        cached=True,
    )


async def classify(
    agent: Agent[None, CommitClassification],
    commit: Commit,
    floor: float,
    cache: Cache,
    model: str,
    threshold: float,
) -> Verdict:
    state = json.dumps(commit.as_state(), ensure_ascii=False)
    key = cache.key(sha=commit.sha, state=state, model=model, threshold=threshold)
    if (entry := cache.get(key, model)) is not None and (hit := _from_entry(commit, entry, floor)) is not None:
        return hit

    started = time.perf_counter()
    try:
        result = await agent.run(state)
    except Exception as exc:  # one bad commit should not sink the run
        return Verdict(commit, None, {}, {}, error=f"{type(exc).__name__}: {exc}")

    details: dict[str, Any] = result.response.provider_details or {}
    verdict = Verdict(
        commit=commit,
        classification=result.output,
        confidence={k: float(v) for k, v in (details.get("confidence") or {}).items()},
        scores={k: float(v) for k, v in (details.get("scores") or {}).items()},
        min_confidence=floor,
        probabilities={
            field: {k: float(v) for k, v in dist.items()}
            for field, dist in (details.get("probabilities") or {}).items()
        },
        model_name=result.response.model_name,
        elapsed=time.perf_counter() - started,
        input_tokens=result.usage.input_tokens or 0,
    )
    cache.put(
        key,
        {
            "sha": commit.sha,
            "subject": commit.subject,
            "model": model,
            "resolved_model": verdict.model_name,
            "output": result.output.model_dump(mode="json"),
            "confidence": verdict.confidence,
            "scores": verdict.scores,
            "probabilities": verdict.probabilities,
            "input_tokens": verdict.input_tokens,
            "elapsed": verdict.elapsed,
        },
    )
    return verdict


async def judge_all(
    commits: list[Commit],
    *,
    model: str,
    threshold: float,
    min_confidence: float,
    concurrency: int,
    progress: Progress | None,
    cache: Cache,
) -> list[Verdict]:
    agent = Agent(
        model,
        output_type=CommitClassification,
        instructions=INSTRUCTIONS,
        model_settings={"typesafe_boolean_threshold": threshold},
    )
    gate = asyncio.Semaphore(max(1, concurrency))
    task = progress.add_task("judging commits", total=len(commits)) if progress else None

    async def one(commit: Commit) -> Verdict:
        async with gate:
            verdict = await classify(agent, commit, min_confidence, cache, model, threshold)
        if progress and task is not None:
            progress.advance(task)
        return verdict

    return await asyncio.gather(*(one(c) for c in commits))


# --- output -----------------------------------------------------------------


# Widest value each column can hold. Without a floor, rich shrinks every column
# to fit a narrow terminal and `compat` arrives as `comp…`, which is worse than
# useless -- the subject is the only column that can lose characters and still
# say something.
MIN_WIDTHS = {
    "commit": 8,
    "type": 8,
    "compat": 9,
    "attention": 10,
    "concern": 7,
    "message": 8,
    "conf": 4,
    "files": 5,
    "sent": 6,
    "ms": 6,
}


def build_table(verdicts: list[Verdict], verbosity: int = 0) -> Table:
    table = Table(box=None, pad_edge=False, header_style="bold", expand=False)

    def fixed(name: str, **kwargs: Any) -> None:
        table.add_column(name, no_wrap=True, min_width=MIN_WIDTHS[name], **kwargs)

    for name in ("commit", "type", "compat", "attention", "concern", "message"):
        fixed(name)
    fixed("conf", justify="right")
    if verbosity >= 1:
        for name in ("files", "sent", "ms"):
            fixed(name, justify="right")
    # the only column allowed to give way when the terminal is narrow
    table.add_column("subject", no_wrap=True, overflow="ellipsis")

    def extra(v: Verdict) -> list[str]:
        if verbosity < 1:
            return []
        truncated = "[yellow]+[/]" if v.commit.diff_truncated else ""
        timing = "[green]cached[/]" if v.cached else f"[dim]{v.elapsed * 1000:.0f}[/]"
        return [
            f"[dim]{len(v.commit.files)}[/]",
            f"[dim]{len(v.commit.diff)}[/]{truncated}",
            timing,
        ]

    for v in verdicts:
        if (c := v.classification) is None:
            table.add_row(v.commit.short, "[red]error[/]", "", "", "", "", "", *extra(v), escape(v.error or ""))
            continue

        def cell(field: str, text: str, style: str = "") -> str:
            if v.unsure(field):
                return "[dim]?[/]"
            return f"[{style}]{text}[/]" if style else text

        attention = int(c.worth_attention)
        table.add_row(
            f"[dim]{v.commit.short}[/]",
            cell("type", c.type),
            cell("compat", c.compat.value, COMPAT_STYLE[c.compat]),
            cell("worth_attention", f"{attention} {c.worth_attention.name}", ATTENTION_STYLE.get(attention, "")),
            cell("single_concern", "[dim]-[/]" if c.single_concern else "[yellow]mixed[/]"),
            cell("message_matching", "[dim]ok[/]" if c.message_matching else "[yellow]mismatch[/]"),
            f"[dim]{v.weakest:.2f}[/]" if v.weakest is not None else "[dim]-[/]",
            *extra(v),
            escape(v.commit.subject),
        )
    return table


def _distribution(dist: dict[str, float], limit: int = 5) -> str:
    """The options the model actually considered, strongest first."""
    live = [(k, p) for k, p in sorted(dist.items(), key=lambda kv: -kv[1]) if p >= 0.005]
    return "  ".join(f"{k} [bold]{p:.0%}[/]" for k, p in live[:limit]) or "[dim]—[/]"


def print_detail(console: Console, verdicts: list[Verdict], verbosity: int) -> None:
    """Per-commit breakdown: every question, its margin, and what else was in play.

    Booleans carry no distribution -- Jev returns one only for pick-one and
    rubric questions -- so those rows show the margin alone.
    """
    for v in verdicts:
        console.print()
        console.print(f"[bold]{v.commit.short}[/] [dim]{escape(v.commit.subject)}[/]")
        if (c := v.classification) is None:
            console.print(f"  [red]{escape(v.error or '')}[/]")
            continue

        answers = {
            "compat": c.compat.value,
            "worth_attention": f"{int(c.worth_attention)} {c.worth_attention.name}",
            "type": c.type,
            "single_concern": "yes" if c.single_concern else "no",
            "message_matching": "yes" if c.message_matching else "no",
        }
        for field, answer in answers.items():
            margin = v.confidence.get(field)
            flag = " [dim](suppressed)[/]" if v.unsure(field) else ""
            shown = f"[bold]{margin:.2f}[/]" if margin is not None else "[dim]—[/]"
            console.print(f"  {field:<17} {answer:<12} {shown}{flag}")
            if dist := v.probabilities.get(field):
                console.print(f"  {'':<17} [dim]{_distribution(dist)}[/]")
        if (score := v.scores.get("worth_attention")) is not None:
            console.print(f"  [dim]unrounded attention {score:.2f}[/]")
        if verbosity >= 3:
            console.print(f"  [dim]state sent ({v.input_tokens} input tokens):[/]")
            state = json.dumps(v.commit.as_state(), ensure_ascii=False, indent=2)
            for line in state.splitlines():
                # a raw dump: no markup parsing, no reflowing of long diff lines
                console.print(Text(f"    {line}", style="dim"), soft_wrap=True)


def write_table(console: Console, table: Table) -> None:
    """Print the table, without padding every row out to the console width.

    Rich pads the final column, which leaves trailing whitespace all over a
    redirected file or a grep. A terminal does not care; a pipe does.
    """
    if console.is_terminal:
        console.print(table)
        return
    with console.capture() as captured:
        console.print(table)
    sys.stdout.write("".join(f"{line.rstrip()}\n" for line in captured.get().splitlines()))


def render_json(verdicts: list[Verdict]) -> str:
    payload = []
    for v in verdicts:
        entry: dict[str, Any] = {
            "sha": v.commit.sha,
            "subject": v.commit.subject,
            "author": v.commit.author,
            "date": v.commit.date,
            "files_changed": len(v.commit.files),
        }
        if (c := v.classification) is None:
            entry["error"] = v.error
        else:
            entry |= {
                "compat": c.compat.value,
                "breaking": c.breaking,
                "worth_attention": int(c.worth_attention),
                "worth_attention_label": c.worth_attention.name,
                "type": c.type,
                "single_concern": c.single_concern,
                "message_matching": c.message_matching,
                "confidence": v.confidence,
                "scores": v.scores,
                "low_confidence": v.low_confidence,
                "cached": v.cached,
            }
        payload.append(entry)
    return json.dumps(payload, indent=2, ensure_ascii=False)


def summarise(verdicts: list[Verdict], verbosity: int = 0) -> str:
    ok = [v for v in verdicts if v.classification]

    def count(field: str, predicate) -> int:
        return sum(1 for v in ok if not v.unsure(field) and predicate(v.classification))

    parts = [
        f"[bold]{len(ok)}[/] judged",
        f"[bold]{count('compat', lambda c: c.breaking)}[/] breaking",
        f"[bold]{count('worth_attention', lambda c: c.worth_attention >= Attention.elevated)}[/] worth attention",
        f"[bold]{count('single_concern', lambda c: not c.single_concern)}[/] doing more than one thing",
        f"[bold]{count('message_matching', lambda c: not c.message_matching)}[/] message mismatch",
    ]
    if unsure := sum(len(v.low_confidence) for v in ok):
        parts.append(f"[bold]{unsure}[/] too close to call")
    if failed := len(verdicts) - len(ok):
        parts.append(f"[bold red]{failed}[/] failed")
    if cached := sum(1 for v in ok if v.cached):
        parts.append(f"[green]{cached}[/] from cache")
    summary = "  ·  ".join(parts)
    if verbosity >= 1 and ok:
        resolved = next((v.model_name for v in ok if v.model_name), "unknown")
        spent = sum(v.input_tokens for v in ok if not v.cached)
        saved = sum(v.input_tokens for v in ok if v.cached)
        slowest = max((v.elapsed for v in ok if not v.cached), default=0.0)
        summary += f"\n[dim]resolved {resolved} · {spent:,} input tokens spent"
        if saved:
            summary += f", {saved:,} saved by cache"
        if slowest:  # nothing was judged when every row came from cache
            summary += f" · slowest commit {slowest * 1000:.0f} ms"
        summary += "[/]"
    return summary


# --- command ----------------------------------------------------------------

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode="rich",
)


@app.command(no_args_is_help=False)
def judge(
    revisions: Annotated[
        str | None,
        typer.Argument(
            metavar="[REVISIONS]",
            help="A pull or merge request URL, or any [bold]git log[/] revision range: "
            "[green]main..feature[/], [green]v1.2.0..[/], [green]HEAD~10..[/], or a bare ref "
            "meaning [green]REF..HEAD[/]. Omitted on a feature branch, judges everything it "
            f"adds on top of the default branch; anywhere else, the last [bold]{DEFAULT_LIMIT}[/] commits.",
        ),
    ] = None,
    repo: Annotated[
        Path, typer.Option("--repo", "-C", help="Run as if started in this directory, like [bold]git -C[/].")
    ] = Path("."),
    base: Annotated[
        str | None,
        typer.Option(
            "--base",
            "-b",
            metavar="REF",
            help="Compare the branch against this instead of the auto-detected default branch.",
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="Cap on commits judged.")] = DEFAULT_LIMIT,
    model: Annotated[
        str, typer.Option("--model", help="Pin a Jev version once thresholds are tuned, e.g. [green]typesafe:jev-1.13.0[/].")
    ] = "typesafe:jev-latest",
    threshold: Annotated[
        float, typer.Option("--threshold", min=0.0, max=1.0, help="Probability bar for a yes on the yes/no questions.")
    ] = 0.5,
    min_confidence: Annotated[
        float,
        typer.Option(
            "--min-confidence",
            min=0.0,
            max=1.0,
            help="Report an answer as [dim]?[/] when its margin falls below this, instead of asserting a coin flip. [green]0[/] reports everything.",
        ),
    ] = DEFAULT_MIN_CONFIDENCE,
    diff_budget: Annotated[
        int, typer.Option("--diff-budget", min=0, help="Max diff characters per commit, spread across its files. [green]0[/] is unlimited.")
    ] = DEFAULT_DIFF_BUDGET,
    exclude: Annotated[
        list[str] | None, typer.Option("--exclude", "-x", metavar="GLOB", help="Pathspec dropped from diffs. Repeatable.")
    ] = None,
    merges: Annotated[bool, typer.Option("--merges", help="Include merge commits, which otherwise have no diff to judge.")] = False,
    concurrency: Annotated[int, typer.Option("--concurrency", "-j", min=1, help="Commits judged in parallel.")] = DEFAULT_CONCURRENCY,
    fail_on: Annotated[
        list[str] | None,
        typer.Option(
            "--fail-on",
            "-f",
            metavar="COND",
            help="Exit [bold]1[/] if any commit matches, e.g. [green]concern=mixed[/], "
            "[green]compat=interface,message=nok[/], [green]attention>=4[/], "
            "[green]type=feat|fix[/]. Shorthands: [green]breaking[/], [green]mixed[/], "
            "[green]mismatch[/], [green]unsure[/]. Repeatable.",
        ),
    ] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Neither read nor write the verdict cache.")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="Re-judge everything and overwrite the cached verdicts.")] = False,
    cache_dir: Annotated[
        Path | None, typer.Option("--cache-dir", metavar="PATH", help="Where verdicts are cached. Defaults under [bold]XDG_CACHE_HOME[/].")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table.")] = False,
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose",
            "-v",
            count=True,
            help="[green]-v[/] adds file counts, diff size sent and timings; "
            "[green]-vv[/] adds every question's margin and probability distribution; "
            "[green]-vvv[/] adds the exact state sent to the model.",
        ),
    ] = 0,
    silent: Annotated[
        bool,
        typer.Option("--silent", "-s", "--quiet", "-q", help="Only the table. No range line, progress or summary."),
    ] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable colour.")] = False,
) -> None:
    """Judge each commit in a range and check whether its message tells the truth about its diff.

    On a feature branch with no range given, judges the whole branch against the default
    branch — the commits a merge request would contain.

    Every commit is put to [bold]Jev[/], TypeSafe's System One model, as five independent
    typed questions: what compatibility it puts at stake, how much reviewer attention it
    deserves, what type it is, whether it does one thing, and whether its message is honest.
    """
    if silent and verbose:
        raise typer.BadParameter("--silent and --verbose contradict each other")

    try:  # fail before spending anything on a condition that cannot be met
        conditions = gate.parse(fail_on or [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--fail-on") from None

    load_dotenv()  # TYPESAFE_API_KEY, if the caller keeps one in .env
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

    err = Console(stderr=True, no_color=no_color, quiet=silent)
    # Why the command failed is never chrome: --silent hides the running
    # commentary, not the reason a pipeline just went red.
    alarm = Console(stderr=True, no_color=no_color)
    out = Console(no_color=no_color)
    if not out.is_terminal:
        out.width = 400  # piped or redirected: never truncate the subject

    def report(message: str) -> None:
        err.print(f"[dim]{escape(message)}[/]")

    cache = Cache.open(cache_dir, enabled=not no_cache, refresh=refresh)

    try:
        # a pull request URL may send us to a different repository than this one
        if revisions is not None and (pull_request := forge.parse_url(revisions)) is not None:
            selection = forge.resolve(
                pull_request,
                revisions,
                start_repo=str(repo),
                cache_dir=cache.directory,
                base=base,
                report=report,
            )
        else:
            selection = plan(str(repo), revisions, limit, base)
        commits = read_commits(
            selection.repo,
            selection,
            limit=limit,
            include_merges=merges,
            exclude=exclude or [],
            diff_budget=diff_budget,
        )
    except GitError as exc:
        alarm.print(f"[bold red]error[/] {escape(str(exc))}")
        raise typer.Exit(EXIT_ERROR) from None

    if not commits:
        err.print(f"[yellow]no commits in[/] {escape(selection.description)}")
        raise typer.Exit(0)

    # the range was very possibly chosen for them, so never leave it implicit
    plural = "" if len(commits) == 1 else "s"
    err.print(f"[dim]{escape(selection.description)}[/] · [bold]{len(commits)}[/] commit{plural}")
    if verbose >= 1:
        err.print(
            f"[dim]{escape(model)} · threshold {threshold} · min-confidence {min_confidence} · "
            f"diff budget {diff_budget or 'unlimited'} · concurrency {concurrency}[/]"
        )

    if verbose >= 1:
        state = "off" if no_cache else ("refreshing" if refresh else str(cache.directory))
        err.print(f"[dim]cache {escape(state)} · questions {cache.fingerprint}[/]")

    show_progress = not as_json and not silent and err.is_terminal
    columns = (
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
    )
    with Progress(*columns, console=err, transient=True, disable=not show_progress) as progress:
        verdicts = asyncio.run(
            judge_all(
                commits,
                model=model,
                threshold=threshold,
                min_confidence=min_confidence,
                concurrency=concurrency,
                progress=progress if show_progress else None,
                cache=cache,
            )
        )

    if as_json:
        print(render_json(verdicts))
    else:
        write_table(out, build_table(verdicts, verbose))
        if verbose >= 2:
            print_detail(err, verdicts, verbose)

    # stdout carries the result, stderr what happened -- so the summary belongs
    # here either way. It is the only place the cache reports what it saved.
    sys.stdout.flush()  # keep what follows in the right order under `2>&1 |`
    err.print()
    err.print(summarise(verdicts, verbose))

    if tripped := gate.evaluate(conditions, verdicts):
        alarm.print()
        for condition, hits in tripped:
            alarm.print(f"[bold red]fail-on[/] [bold]{escape(condition.source)}[/] matched {len(hits)}:")
            for verdict in hits:
                alarm.print(f"  [dim]{verdict.commit.short}[/] {escape(verdict.commit.subject)}")
        raise typer.Exit(EXIT_GATE)

    raise typer.Exit(EXIT_UNJUDGED if any(v.error for v in verdicts) else 0)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
