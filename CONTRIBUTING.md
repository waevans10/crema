# Contributing to crema

Thanks for taking a look! crema is a small, focused project — a self-hosted
GaggiMate shot reviewer — and contributions of all sizes are welcome.

## Getting set up

Requires Python 3.13+. From the project root:

```bash
uv sync                 # installs the app + test dependencies
```

(`uv sync` installs the `dev` dependency group — pytest and pytest-asyncio —
automatically. With plain pip: `pip install -e . && pip install pytest pytest-asyncio`.)

You don't need a GaggiMate machine or an Anthropic API key to work on most of the
code or to run the tests — those are only needed to actually pull shots and run a
live review.

## Running the tests

```bash
uv run pytest           # or: pytest
```

The tests in [`tests/`](tests/) are **offline** — they exercise the pure pieces
of the pipeline (prompt building, the structured response schemas, and the
device-safe clamping applied to drafted profiles) with no machine, network, or
API key. Please keep new tests offline where you can, and add one alongside any
change to that plumbing.

## How the code is laid out

- `src/crema/` — the application:
  - `cli.py` — the `crema` command (Typer).
  - `config.py` — settings, all from `.env`.
  - `ingest.py` — pull + parse shots from the device; cache profiles.
  - `review.py` — the Claude review call.
  - `draft.py` — draft a profile edit from a review, clamp + validate it.
  - `push.py` — write an approved edit to the machine over WebSocket.
  - `prompts.py` — system prompts and the structured shapes Claude returns.
  - `db.py` — SQLite storage.
  - `notify.py` — optional Discord webhook.
  - `web/app.py` — the FastAPI web report.
- `src/gaggimate_mcp/` — **vendored** from the MIT-licensed
  [`gaggimate-mcp`](https://github.com/julianleopold/gaggimate-mcp) project (the
  `.slog` parser + device HTTP/WebSocket clients). Prefer not to edit this
  directly; if the parser needs changes, consider upstreaming them.

The design notes and rationale live in [`SCOPE.md`](SCOPE.md).

## A few conventions

- The LLM calls live in exactly two places — `review.py` and `draft.py`. Both use
  the Anthropic SDK with a Pydantic-typed structured response.
- Anything that could reach the machine should degrade gracefully when it's off:
  browsing, past reviews, and drafting all work against cached data.
- Profile changes are always **suggest → draft → approve → push**, and a push
  creates a new `[AI]` profile — never overwrite a user's original.

## Pull requests

Open an issue first for anything larger than a small fix, so we can talk through
the approach. For small fixes, a PR with a clear description is perfect. Please
run the tests before submitting.
