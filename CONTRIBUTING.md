# Contributing

## Development setup

```bash
git clone https://github.com/thejoeejoee/git-judge-commits
cd git-judge-commits
uv sync
export TYPESAFE_API_KEY=...   # or drop it in .env, which is loaded automatically
uv run git-judge-commits --help
```

The package targets Python 3.11+. `StrEnum` in `model.py` is the floor; nothing else
here needs anything newer.

## Layout

| File | What lives there |
| --- | --- |
| `model.py` | The five typed questions, their descriptions and rubrics, and the instructions. **The whole prompt is here.** |
| `git.py` | Reading commits, resolving revision ranges, detecting the default branch, budgeting diffs |
| `cli.py` | Typer command, classification, rendering, verbosity |
| `cache.py` | Verdict cache and its key |
| `forge.py` | Pull/merge request URLs: parsing, fetching, and finding the base |
| `gate.py` | `--fail-on` conditions |
| `action.yml`, `scripts/report.py` | The GitHub Action, its comment and job summary |

## Changing the questions

`model.py` is hashed into the cache key, so editing a field description or a rubric
docstring automatically invalidates every cached verdict that depended on it. You do not
need to bump a version or clear anything by hand.

Two things to know before you edit it:

- **A field description is the question.** Jev takes the prompt as only the thing being
  judged, so anything you want asked has to live on the output type.
- **The rubric lives in enum member docstrings**, which never reach the JSON schema.
  That is why the cache fingerprints the file's *source* rather than its schema.

After changing a question, re-measure rather than eyeballing it. Per-field confidence is
the tool for this — a field whose margin sits near zero is a badly posed question, not an
unsure model:

```bash
git-judge-commits HEAD~25.. --json | python -c "
import json, statistics as st, sys
rows = [c for c in json.load(sys.stdin) if 'confidence' in c]
for field in rows[0]['confidence']:
    values = [r['confidence'][field] for r in rows]
    print(f'{field:18} mean {st.mean(values):.2f}  median {st.median(values):.2f}')
"
```

Splitting `breaking: bool` into the five-way `compat` choice came out of exactly this:
it moved mean confidence from 0.54 (a coin flip) to 0.77.

## Regenerating the screenshots

The images in the README are real runs, not mock-ups. Markdown cannot render ANSI, so
each one is captured with colour forced on and re-rendered to SVG:

```bash
DEMO_REAL_REPO=~/src/some-repo DEMO_LIAR_REPO=/tmp/demo-repo uv run python docs/record.py
```

`DEMO_LIAR_REPO` should be a small throwaway repo containing a commit whose message
does not describe its diff.

Those SVGs position each coloured run with an explicit `textLength`, which browsers
honour, so they stay aligned on GitHub even though the embedded webfont is blocked
there. Local renderers that ignore `textLength` (`rsvg-convert`, for one) will show
columns overlapping — check in a browser, not a thumbnailer.

## Releasing

```bash
uv build
uv publish
```
