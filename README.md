# Token usage report

Generates a single HTML page showing your Claude Code and Codex token usage,
built entirely from the transcripts already on your machine. Nothing is sent
anywhere.

## Usage

Needs Python 3.11 or newer and nothing else.

```sh
python3 claude_usage_report.py --open
```

This writes `token-usage-report.html` in the current directory and opens it.

Options:

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--output` | `token-usage-report.html` | Where to write the report |
| `--days` | `30` | Active days shown in the daily chart and table |
| `--claude-dir` | `~/.claude/projects` | Claude Code transcripts |
| `--codex-dir` | `~/.codex/sessions` | Codex transcripts |
| `--no-codex` | off | Leave out the Codex section |
| `--open` | off | Open the report in a browser when done |

## What it shows

For Claude Code and, if present, Codex:

- today's and the last 7 days' tokens
- all-time best day for input, output and cache read, plus all-time totals
- a daily chart and a monthly chart (input and output as bars, cache read on
  the right axis, because it is far larger)
- output tokens by model, per month
- a monthly table that expands into weeks and models
- a daily table

Input includes cache writes. Days are in your local time zone.

## How tokens are counted

Claude Code writes one transcript line per part of a response, each
repeating that response's usage. The report counts each API call once, keyed
by message and request id, using the largest value seen for each count.
Subagent transcripts are included. Codex records are counted once per
response id, and the model is taken from the session's turn context.

The figures only cover transcripts on this machine. Claude Code removes old
transcripts after `cleanupPeriodDays` (30 days unless you have changed it), so
history before that may be missing.

The charts load Plotly from `cdn.plot.ly`, so viewing them needs an internet
connection; the tables work without one.

## Tests

```sh
python3 -m unittest
```
