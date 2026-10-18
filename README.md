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
| `--no-cost` | off | Leave out the API cost estimate |
| `--budget` | none | Monthly API budget in dollars; drawn on the cost charts, with how many days it lasts at this month's rate |
| `--compare-model` | cheapest Opus on the pricing page | Also price every call as if it ran on this model |
| `--pricing-file` | none | Read prices from a saved copy of the pricing page instead of fetching it |
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
- for Claude Code, what the usage would cost at API prices: this month so
  far and projected, the same projection as if every call had used the
  comparison model, the most expensive day, the all-time total, cost by
  month and model, and where the cost goes (cache reads, cache writes,
  output, uncached input)

Input includes cache writes. Days are in your local time zone.

## API cost estimate

Prices are not built into the script. Each run downloads
<https://platform.claude.com/docs/en/about-claude/pricing.md> and reads the
model price table from it, so new models and price changes are picked up
without updating the script: a row such as "Claude Opus 6" becomes
`claude-opus-6`, the id Claude Code records. Long-prompt tiers written as
"for prompts over N tokens" are applied per call.

Every call is priced at the model it actually used, with cache writes split
into the 5 minute and 1 hour rates from the transcript. A successful
download is saved to `~/.cache/claude-usage-report/pricing.md`; if a later
download fails, that copy is used and the report says so. With neither, the
report is produced without the cost section. Calls on models the page does
not list are left out of the cost and counted in a note.

The estimate uses standard first-party prices. It does not include
negotiated discounts, Bedrock or Vertex pricing, US-only inference, batch
discounts or server tool fees such as web search. Codex is not priced.

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
