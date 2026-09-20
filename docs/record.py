"""Regenerate the terminal screenshots in the README.

Markdown cannot render ANSI, so each example is run for real, its coloured output
captured, and re-rendered to an SVG that GitHub will display.

    uv run python docs/record.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

# Cursor moves and carriage returns left behind by the erased progress bar: a
# line made only of these looks non-empty to str.strip() but renders as a gap.
CONTROL = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")

DOCS = Path(__file__).parent
WIDTH = 118  # a wide-but-ordinary terminal


def record(name: str, title: str, args: list[str], width: int = WIDTH) -> None:
    environment = os.environ | {"FORCE_COLOR": "1", "COLUMNS": str(width), "TERM": "xterm-256color"}
    process = subprocess.run(
        [sys.executable, "-m", "git_judge_commits", *args],
        capture_output=True,
        text=True,
        env=environment,
    )
    # FORCE_COLOR makes rich treat the pipe as a terminal, so the transient
    # progress bar is drawn into the capture. It is noise in a still image.
    stderr = "\n".join(
        line for line in process.stderr.splitlines() if "judging commits" not in line
    )
    # stderr carries the range line and summary, stdout the table; the reader
    # sees them interleaved, so stitch them back in that order.
    # everything from the summary line onward belongs after the table; at -v the
    # header is several lines, so split on the summary rather than on the first line.
    lines = stderr.splitlines()
    cut = next((i for i, line in enumerate(lines) if " judged" in line), len(lines))
    visible = [line for line in lines[:cut] if CONTROL.sub("", line).strip()]
    head = "\n".join(visible)
    tail = "\n".join(lines[cut:]).strip()
    body = f"{head}\n{process.stdout.rstrip()}\n\n{tail}".strip("\n")
    # the erased progress bar leaves blank lines behind; one is enough
    body = re.sub(r"\n{3,}", "\n\n", body)

    console = Console(record=True, width=width, file=open(os.devnull, "w"))
    console.print(Text.from_ansi(body))
    console.save_svg(str(DOCS / f"{name}.svg"), title=title)
    print(f"{name}.svg  ({len(body.splitlines())} lines)")


def main() -> None:
    liar = os.environ["DEMO_LIAR_REPO"]
    real = os.environ["DEMO_REAL_REPO"]
    record("demo", "git-judge-commits", ["HEAD~8..", "-C", real, "-x", "*.lock"], width=132)
    record("mismatch", "a commit that lies about itself", ["base..", "-C", liar], width=96)
    record("verbose", "git-judge-commits -vv", ["HEAD~2..", "-C", real, "-vv", "-x", "*.lock"], width=140)
    record("cache", "re-running costs nothing", ["HEAD~8..", "-C", real, "-v", "-x", "*.lock"], width=132)


if __name__ == "__main__":
    main()
