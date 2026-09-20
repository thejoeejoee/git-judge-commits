"""Turn the verdicts JSON into a job summary, outputs, and the step's exit code.

Run by action.yml. Kept out of the package because it belongs to the action,
not to the tool, and runs on whatever python3 the runner has.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ATTENTION_MARK = {0: "·", 1: "·", 2: "", 3: "⚠️", 4: "⚠️", 5: "🔴"}
COMPAT_MARK = {"interface": "💥", "data": "💥", "behaviour": "", "internal": "", "none": ""}

EXIT_GATE = 1
EXIT_UNJUDGED = 3


def cell(record: dict, field: str, text: str) -> str:
    """A field the gate would have skipped is shown as unknown, not asserted."""
    return "`?`" if field in record.get("low_confidence", []) else text


def table(records: list[dict]) -> list[str]:
    rows = ["| commit | type | compat | attention | concern | message | subject |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in records:
        if "error" in r:
            rows.append(f"| `{r['sha'][:8]}` | ❌ | | | | | {r['error']} |")
            continue
        attention = r["worth_attention"]
        rows.append(
            "| `{sha}` | {type} | {compat} | {attention} | {concern} | {message} | {subject} |".format(
                sha=r["sha"][:8],
                type=cell(r, "type", r["type"]),
                compat=cell(r, "compat", f"{COMPAT_MARK.get(r['compat'], '')} {r['compat']}".strip()),
                attention=cell(r, "worth_attention", f"{ATTENTION_MARK.get(attention, '')} {attention} {r['worth_attention_label']}".strip()),
                concern=cell(r, "single_concern", "—" if r["single_concern"] else "🧩 mixed"),
                message=cell(r, "message_matching", "ok" if r["message_matching"] else "🚩 **mismatch**"),
                subject=r["subject"].replace("|", "\\|"),
            )
        )
    return rows


def main() -> int:
    status = int(os.environ.get("STATUS") or 0)
    path = Path(os.environ["JSON"])

    try:
        records = json.loads(path.read_text())
    except (OSError, ValueError):
        # The run failed before writing anything -- a bad argument, or git could
        # not resolve the range. Its own message is already in the log.
        return status or 1

    judged = [r for r in records if "error" not in r]

    def settled(record: dict, field: str) -> bool:
        return field not in record.get("low_confidence", [])

    counts = {
        "judged": len(judged),
        "breaking": sum(1 for r in judged if r["breaking"] and settled(r, "compat")),
        "mismatched": sum(1 for r in judged if not r["message_matching"] and settled(r, "message_matching")),
        "mixed": sum(1 for r in judged if not r["single_concern"] and settled(r, "single_concern")),
        "failed": "true" if status == EXIT_GATE else "false",
    }
    with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
        for key, value in counts.items():
            handle.write(f"{key}={value}\n")

    headline = (
        f"{counts['judged']} judged · {counts['breaking']} breaking · "
        f"{counts['mixed']} doing more than one thing · {counts['mismatched']} message mismatch"
    )
    print(headline)

    verdict = "🚩 some commits need another look" if status == EXIT_GATE else "✅ nothing flagged"
    body = [f"## ⚖️ git-judge-commits", "", f"**{verdict}** — {headline}", ""]
    body += table(records) if records else ["_No commits in range._"]
    body += ["", "<sub>Judged by [Jev](https://typesafe.ai), which answers typed questions with "
             "calibrated probabilities. `?` means the answer was too close to call.</sub>"]
    markdown = "\n".join(body) + "\n"

    if os.environ.get("SUMMARY", "true").lower() == "true" and (summary := os.environ.get("GITHUB_STEP_SUMMARY")):
        with open(summary, "a") as handle:
            handle.write(markdown)

    # Written whether or not it gets posted, so the next step only has to decide.
    if temp := os.environ.get("RUNNER_TEMP"):
        comment = Path(temp) / "git-judge-commits-comment.md"
        comment.write_text(f"{os.environ.get('MARKER', '')}\n{markdown}")
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write(f"comment={comment}\n")

    if status == EXIT_UNJUDGED:
        print("::warning::some commits could not be judged")
    return status


if __name__ == "__main__":
    sys.exit(main())
