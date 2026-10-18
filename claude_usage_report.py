#!/usr/bin/env python3
"""Generate an HTML report of your Claude Code and Codex token usage.

Reads local transcripts only (~/.claude/projects and ~/.codex/sessions) and
writes a single HTML file. Nothing is uploaded anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

FIELDS = ("input", "cache_write", "cache_read", "output")


@dataclass
class Usage:
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    calls: int = 0

    def add(self, other: Usage) -> None:
        for name in FIELDS:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.calls += other.calls

    def input_total(self) -> int:
        return self.input + self.cache_write

    def as_dict(self) -> dict:
        return {
            "in": self.input_total(),
            "out": self.output,
            "read": self.cache_read,
            "calls": self.calls,
        }


@dataclass
class Call:
    day: str
    model: str
    usage: Usage = field(default_factory=Usage)


def local_day(timestamp: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.date().isoformat()


def iter_jsonl(root: Path):
    if not root.is_dir():
        return
    for dirpath, _, files in os.walk(root):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            try:
                with open(
                    os.path.join(dirpath, name), encoding="utf-8", errors="replace"
                ) as fh:
                    yield name, fh
            except OSError:
                continue


def parse_claude_line(line: str):
    if "input_tokens" not in line:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict) or "input_tokens" not in usage:
        return None
    model = message.get("model") or "unknown"
    if model.startswith("<"):
        return None
    day = local_day(record.get("timestamp") or "")
    if day is None:
        return None
    message_id = message.get("id")
    key = (message_id, record.get("requestId")) if message_id else None
    values = Usage(
        input=usage.get("input_tokens") or 0,
        cache_write=usage.get("cache_creation_input_tokens") or 0,
        cache_read=usage.get("cache_read_input_tokens") or 0,
        output=usage.get("output_tokens") or 0,
        calls=1,
    )
    return key, day, model, values


def scan_claude(root: Path) -> list[Call]:
    calls: dict = {}
    anonymous: list[Call] = []
    for _, fh in iter_jsonl(root):
        for line in fh:
            parsed = parse_claude_line(line)
            if parsed is None:
                continue
            key, day, model, values = parsed
            if key is None:
                anonymous.append(Call(day, model, values))
                continue
            existing = calls.get(key)
            if existing is None:
                calls[key] = Call(day, model, values)
                continue
            for name in FIELDS:
                setattr(
                    existing.usage,
                    name,
                    max(getattr(existing.usage, name), getattr(values, name)),
                )
    return list(calls.values()) + anonymous


def scan_codex_lines(lines, seen: set[str]) -> list[Call]:
    calls: list[Call] = []
    model = "unknown"
    for line in lines:
        if '"turn_context"' not in line and "token_usage_record" not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        payload = record.get("payload") or {}
        kind = record.get("type")
        if kind == "turn_context":
            model = payload.get("model") or model
            continue
        if kind != "token_usage_record":
            continue
        response_id = payload.get("response_id")
        if response_id:
            if response_id in seen:
                continue
            seen.add(response_id)
        usage = payload.get("usage") or {}
        day = local_day(record.get("timestamp") or "")
        if day is None:
            continue
        total_input = usage.get("input_tokens") or 0
        cached = usage.get("cached_input_tokens") or 0
        calls.append(
            Call(
                day,
                model,
                Usage(
                    input=max(total_input - cached, 0),
                    cache_write=usage.get("cache_write_input_tokens") or 0,
                    cache_read=cached,
                    output=usage.get("output_tokens") or 0,
                    calls=1,
                ),
            )
        )
    return calls


def scan_codex(root: Path) -> list[Call]:
    seen: set[str] = set()
    calls: list[Call] = []
    for _, fh in iter_jsonl(root):
        calls.extend(scan_codex_lines(fh, seen))
    return calls


def summarise(name: str, calls: list[Call], today: date) -> dict | None:
    if not calls:
        return None
    daily: dict[str, Usage] = defaultdict(Usage)
    months: dict[str, Usage] = defaultdict(Usage)
    weeks: dict[str, dict[int, Usage]] = defaultdict(lambda: defaultdict(Usage))
    models: dict[str, dict[str, Usage]] = defaultdict(lambda: defaultdict(Usage))
    total = Usage()
    for call in calls:
        ym = call.day[:7]
        daily[call.day].add(call.usage)
        months[ym].add(call.usage)
        week = date.fromisoformat(call.day).isocalendar()[1]
        weeks[ym][week].add(call.usage)
        models[ym][call.model].add(call.usage)
        total.add(call.usage)

    records = {}
    for label, pick in (
        ("in", Usage.input_total),
        ("out", lambda u: u.output),
        ("read", lambda u: u.cache_read),
    ):
        best_day = max(daily, key=lambda d: pick(daily[d]))
        records[label] = [best_day, pick(daily[best_day])]

    week_cutoff = (today - timedelta(days=7)).isoformat()
    last7 = Usage()
    for day, usage in daily.items():
        if day > week_cutoff:
            last7.add(usage)

    return {
        "name": name,
        "first_day": min(daily),
        "today": daily.get(today.isoformat(), Usage()).as_dict(),
        "last7": last7.as_dict(),
        "total": total.as_dict(),
        "records": records,
        "daily": [[d, *daily[d].as_dict().values()] for d in sorted(daily)],
        "months": [
            {
                "ym": ym,
                **months[ym].as_dict(),
                "weeks": [
                    {"week": wk, **weeks[ym][wk].as_dict()}
                    for wk in sorted(weeks[ym], reverse=True)
                ],
                "models": {m: u.as_dict() for m, u in sorted(models[ym].items())},
            }
            for ym in sorted(months, reverse=True)
        ],
    }


def render(sections: list[dict], days: int, generated: str) -> str:
    data = json.dumps({"sections": sections, "days": days, "generated": generated})
    for char, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        data = data.replace(char, escaped)
    return TEMPLATE.replace("__DATA__", data)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Token Usage Report</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" integrity="sha384-cCVCZkAjYNxaYKbM8lsArLznDF/SvMFr1jcZrvOpSTCa0W40ZAdLzHCEulnUa5i7" crossorigin="anonymous"></script>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #1a1a2e; color: #e0e0e0; margin: 0; padding: 20px 16px; }
.container { max-width: 1000px; margin: 0 auto; }
h1 { color: #d4a574; border-bottom: 2px solid #333; padding-bottom: 10px; }
h2 { color: #d4a574; border-top: 2px solid #333; padding-top: 25px; margin-top: 40px; font-size: 1.6em; }
h3 { color: #d4a574; margin: 22px 0 6px; font-size: 1.05em; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 12px 0; }
.card { background: #16213e; border-radius: 10px; padding: 16px; text-align: center; }
.card .value { font-size: 1.9em; font-weight: bold; }
.card .label { color: #888; margin-top: 5px; font-size: 0.85em; }
.chart { border-radius: 10px; margin: 8px 0 18px; overflow: hidden; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; background: #16213e; border-radius: 10px; overflow: hidden; margin: 8px 0; }
th { background: #0f3460; padding: 10px 12px; text-align: left; color: #d4a574; font-size: 0.85em; white-space: nowrap; }
td { padding: 7px 12px; border-top: 1px solid #1a1a2e; font-size: 0.9em; white-space: nowrap; }
tr.month { cursor: pointer; }
tr.month:hover, tr:hover { background: #1a2a4e; }
tr.sub td { color: #aaa; font-size: 0.85em; background: #0d1a30; }
tr.sub td:first-child { padding-left: 30px; }
.arrow { font-size: 0.7em; color: #888; display: inline-block; transition: transform 0.2s; }
.arrow.open { transform: rotate(90deg); }
.note { color: #777; font-size: 0.85em; }
.footer { color: #555; font-size: 0.8em; margin: 30px 0 10px; text-align: center; }
</style></head><body>
<div class="container" id="root"></div>
<script type="application/json" id="report-data">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('report-data').textContent);
const C = { in: '#4a90d9', out: '#d4a574', read: '#66bb6a' };
const PALETTE = ['#d4a574', '#4a90d9', '#66bb6a', '#ba68c8', '#ef5350', '#26c6da', '#ffca28', '#8d6e63', '#78909c', '#ec407a', '#9ccc65', '#5c6bc0', '#ff7043', '#26a69a', '#d4e157', '#f48fb1', '#90caf9', '#a1887f'];
const BASE = {
  paper_bgcolor: '#16213e', plot_bgcolor: '#16213e',
  font: { color: '#e0e0e0', family: '-apple-system, sans-serif' },
  legend: { orientation: 'h', y: 1.14, x: 0 }, height: 320,
};
const CONFIG = { responsive: true, displayModeBar: false };

function fmt(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return Math.round(n / 1e3) + 'K';
  return String(n);
}
function el(tag, attrs, html) {
  const e = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([k, v]) => e.setAttribute(k, v));
  if (html !== undefined) e.innerHTML = html;
  return e;
}
function text(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function dayLabel(d) {
  return new Date(d + 'T12:00:00').toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' });
}
function monthLabel(ym) {
  return new Date(ym + '-15T12:00:00').toLocaleDateString('en-GB', { month: 'long', year: 'numeric' });
}
function cards(parent, items) {
  const box = el('div', { class: 'cards' });
  items.forEach(([value, label, colour]) => {
    box.appendChild(el('div', { class: 'card' },
      `<div class="value" style="color:${colour || '#d4a574'}">${value}</div><div class="label">${label}</div>`));
  });
  parent.appendChild(box);
}
function chart(parent, traces, layout) {
  const div = el('div', { class: 'chart' });
  parent.appendChild(div);
  if (window.Plotly) Plotly.newPlot(div, traces, Object.assign({}, BASE, layout), CONFIG);
  else div.innerHTML = '<p class="note">Charts need an internet connection to load Plotly.</p>';
}
function tokenTraces(x, rows, hoverFmt) {
  return [
    { x, y: rows.map(r => r.in), name: 'Input (+ cache write)', type: 'bar', marker: { color: C.in },
      hovertemplate: `<b>%{x${hoverFmt}}</b><br>Input: %{y:,.0f}<extra></extra>` },
    { x, y: rows.map(r => r.out), name: 'Output', type: 'bar', marker: { color: C.out },
      hovertemplate: `<b>%{x${hoverFmt}}</b><br>Output: %{y:,.0f}<extra></extra>` },
    { x, y: rows.map(r => r.read), name: 'Cache read (right axis)', type: 'scatter', mode: 'lines+markers',
      yaxis: 'y2', line: { color: C.read, width: 2 },
      hovertemplate: `<b>%{x${hoverFmt}}</b><br>Cache read: %{y:,.0f}<extra></extra>` },
  ];
}
function tokenLayout(tickformat, dtick) {
  return {
    barmode: 'group', margin: { l: 60, r: 60, t: 10, b: 50 },
    xaxis: { gridcolor: '#2a3a5e', type: 'date', tickformat, dtick, tickangle: -45, automargin: true },
    yaxis: { gridcolor: '#2a3a5e', title: 'input / output', exponentformat: 'B', rangemode: 'tozero' },
    yaxis2: { overlaying: 'y', side: 'right', title: 'cache read', exponentformat: 'B', showgrid: false, rangemode: 'tozero' },
  };
}
function tokenCells(u) { return `<td>${fmt(u.in)}</td><td>${fmt(u.out)}</td><td>${fmt(u.read)}</td><td>${u.calls.toLocaleString('en-GB')}</td>`; }

function renderSection(root, s) {
  root.appendChild(el('h2', {}, `${text(s.name)} usage`));
  root.appendChild(el('p', { class: 'note' }, `Tracking since ${dayLabel(s.first_day)}.`));

  root.appendChild(el('h3', {}, 'Today and last 7 days'));
  cards(root, [
    [fmt(s.today.in), 'Input today (+ cache write)', C.in],
    [fmt(s.today.out), 'Output today', C.out],
    [fmt(s.today.read), 'Cache read today', C.read],
    [fmt(s.last7.in), '7 day input'], [fmt(s.last7.out), '7 day output'], [fmt(s.last7.read), '7 day cache read'],
  ]);

  root.appendChild(el('h3', {}, 'All-time records and totals'));
  cards(root, [
    [fmt(s.records.in[1]), `Best day input<br>${dayLabel(s.records.in[0])}`, C.in],
    [fmt(s.records.out[1]), `Best day output<br>${dayLabel(s.records.out[0])}`, C.out],
    [fmt(s.records.read[1]), `Best day cache read<br>${dayLabel(s.records.read[0])}`, C.read],
    [fmt(s.total.in), 'All-time input'], [fmt(s.total.out), 'All-time output'], [fmt(s.total.read), 'All-time cache read'],
  ]);

  const recent = s.daily.slice(-DATA.days).map(([day, i, o, r, calls]) => ({ day, in: i, out: o, read: r, calls }));
  root.appendChild(el('h3', {}, `Daily tokens (last ${DATA.days} active days)`));
  chart(root, tokenTraces(recent.map(r => r.day), recent, ''), tokenLayout('%d %b', undefined));

  const months = s.months.slice().reverse();
  root.appendChild(el('h3', {}, 'Tokens by month'));
  chart(root, tokenTraces(months.map(m => m.ym), months, '|%B %Y'), tokenLayout('%b %Y', 'M1'));

  const modelNames = [...new Set(s.months.flatMap(m => Object.keys(m.models)))].sort();
  root.appendChild(el('h3', {}, 'Output tokens by model'));
  chart(root, modelNames.map((name, i) => ({
    x: months.map(m => m.ym), y: months.map(m => (m.models[name] || { out: 0 }).out),
    name: text(name), type: 'bar', marker: { color: PALETTE[i % PALETTE.length] },
    hovertemplate: `<b>%{x|%B %Y}</b><br>${text(name)}: %{y:,.0f}<extra></extra>`,
  })), {
    barmode: 'stack', margin: { l: 60, r: 20, t: 10, b: 50 }, height: 340,
    xaxis: { gridcolor: '#2a3a5e', type: 'date', tickformat: '%b %Y', dtick: 'M1', tickangle: -45, automargin: true },
    yaxis: { gridcolor: '#2a3a5e', title: 'output tokens', exponentformat: 'B' },
    legend: { orientation: 'h', y: -0.35, x: 0 },
  });

  root.appendChild(el('h3', {}, 'Monthly summary'));
  root.appendChild(el('p', { class: 'note' }, 'Click a month to show its weeks and models.'));
  const wrap = el('div', { class: 'table-wrap' });
  const table = el('table', {}, '<tr><th>Month</th><th>Input</th><th>Output</th><th>Cache read</th><th>API calls</th></tr>');
  s.months.forEach(m => {
    const id = `${s.name}-${m.ym}`.replace(/[^A-Za-z0-9-]/g, '');
    const row = el('tr', { class: 'month' }, `<td>${monthLabel(m.ym)} <span class="arrow" id="a-${id}">&#9654;</span></td>${tokenCells(m)}`);
    row.addEventListener('click', () => {
      const subs = table.querySelectorAll(`.s-${id}`);
      const show = subs.length && subs[0].style.display === 'none';
      subs.forEach(r => { r.style.display = show ? '' : 'none'; });
      document.getElementById(`a-${id}`).classList.toggle('open', show);
    });
    table.appendChild(row);
    m.weeks.forEach(w => {
      table.appendChild(el('tr', { class: `sub s-${id}`, style: 'display:none' }, `<td>Week ${w.week}</td>${tokenCells(w)}`));
    });
    Object.entries(m.models).sort((a, b) => b[1].out - a[1].out).forEach(([name, u]) => {
      table.appendChild(el('tr', { class: `sub s-${id}`, style: 'display:none' }, `<td>${text(name)}</td>${tokenCells(u)}`));
    });
  });
  wrap.appendChild(table);
  root.appendChild(wrap);

  root.appendChild(el('h3', {}, 'Daily breakdown'));
  const dwrap = el('div', { class: 'table-wrap' });
  const dtable = el('table', {}, '<tr><th>Date</th><th>Input</th><th>Output</th><th>Cache read</th><th>API calls</th></tr>');
  recent.slice().reverse().forEach(r => {
    dtable.appendChild(el('tr', {}, `<td>${dayLabel(r.day)}</td>${tokenCells(r)}`));
  });
  dwrap.appendChild(dtable);
  root.appendChild(dwrap);
}

const root = document.getElementById('root');
root.appendChild(el('h1', {}, 'Token Usage Report'));
root.appendChild(el('p', { class: 'note' },
  'Built from local Claude Code and Codex transcripts. Input includes cache writes. Each API call is counted once.'));
if (!DATA.sections.length) root.appendChild(el('p', {}, 'No Claude Code or Codex transcripts were found.'));
DATA.sections.forEach(s => renderSection(root, s));
root.appendChild(el('div', { class: 'footer' }, `Generated ${text(DATA.generated)}`));
</script></body></html>
"""


def main(argv: list[str] | None = None) -> int:
    home = Path.home()
    parser = argparse.ArgumentParser(
        description="Generate an HTML report of Claude Code and Codex token usage from local transcripts."
    )
    parser.add_argument(
        "-o", "--output", default="token-usage-report.html", help="output HTML file"
    )
    parser.add_argument(
        "--claude-dir", default=home / ".claude" / "projects", type=Path
    )
    parser.add_argument("--codex-dir", default=home / ".codex" / "sessions", type=Path)
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="active days shown in the daily chart and table",
    )
    parser.add_argument(
        "--no-codex", action="store_true", help="leave out the Codex section"
    )
    parser.add_argument(
        "--open", action="store_true", help="open the report in a browser when done"
    )
    args = parser.parse_args(argv)

    today = date.today()
    sections = []
    claude = summarise("Claude Code", scan_claude(args.claude_dir), today)
    if claude:
        sections.append(claude)
    if not args.no_codex:
        codex = summarise("Codex", scan_codex(args.codex_dir), today)
        if codex:
            sections.append(codex)

    output = Path(args.output)
    output.write_text(
        render(sections, args.days, datetime.now().strftime("%d %b %Y %H:%M")),
        encoding="utf-8",
    )
    for s in sections:
        print(
            f"{s['name']}: {fmt_count(s['total']['calls'])} API calls since {s['first_day']}"
        )
    print(f"Wrote {output.resolve()}")
    if args.open:
        webbrowser.open(output.resolve().as_uri())
    return 0


def fmt_count(n: int) -> str:
    return f"{n:,}"


if __name__ == "__main__":
    sys.exit(main())
