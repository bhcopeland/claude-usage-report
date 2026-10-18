#!/usr/bin/env python3
"""Generate an HTML report of your Claude Code and Codex token usage.

Reads local transcripts only (~/.claude/projects and ~/.codex/sessions) and
writes a single HTML file. Nothing is uploaded anywhere.
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import re
import sys
import urllib.request
import webbrowser
from collections import defaultdict
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta
from pathlib import Path

TOKEN_FIELDS = ("input", "cache_write", "cache_write_1h", "cache_read", "output")

PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
PRICING_CACHE = Path.home() / ".cache" / "claude-usage-report" / "pricing.md"
MODEL_ALIASES = {
    "claude-3-5-haiku": "claude-haiku-3-5",
    "claude-opus-4-0": "claude-opus-4",
    "claude-sonnet-4-0": "claude-sonnet-4",
}
PRICE_COLUMNS = (
    "base input",
    "5m cache write",
    "1h cache write",
    "cache hit",
    "output",
)


@dataclass
class Pricing:
    models: dict = field(default_factory=dict)
    long_prompt: dict = field(default_factory=dict)
    source: str = ""


def model_id_from_name(name: str) -> tuple[str, int | None]:
    clean = re.sub(r"<[^>]+>", "", name)
    clean = re.sub(r"\(\[[^\]]*\]\([^)]*\)\)", "", clean)
    clean = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", clean)
    over = re.search(r"prompts over ([\d,]+) tokens", clean)
    threshold = int(over.group(1).replace(",", "")) if over else None
    clean = re.sub(r"\([^)]*\)", "", clean).strip().lower()
    return "-".join(re.split(r"[\s.]+", clean)), threshold


def parse_pricing(markdown: str, source: str) -> Pricing:
    pricing = Pricing(source=source)
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        header = [c.strip().lower() for c in line.strip().strip("|").split("|")]
        if not line.lstrip().startswith("|") or not all(
            any(col in h for h in header) for col in PRICE_COLUMNS
        ):
            continue
        columns = [
            next(i for i, h in enumerate(header) if col in h) for col in PRICE_COLUMNS
        ]
        for row in lines[index + 2 :]:
            if not row.lstrip().startswith("|"):
                break
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if len(cells) < len(header):
                continue
            amounts = []
            for col in columns:
                match = re.search(r"\$\s*([\d.]+)", cells[col])
                if not match:
                    break
                amounts.append(float(match.group(1)))
            model, threshold = model_id_from_name(cells[0])
            if len(amounts) != len(columns) or not model.startswith("claude-"):
                continue
            if threshold is None:
                pricing.models.setdefault(model, tuple(amounts))
            else:
                pricing.long_prompt[model] = (threshold, tuple(amounts))
        break
    return pricing


def default_compare_model(pricing: Pricing) -> str | None:
    opus = [m for m in pricing.models if m.startswith("claude-opus-")]
    if not opus:
        return None
    return min(
        opus,
        key=lambda m: (
            pricing.models[m][0],
            pricing.models[m][4],
            [-ord(c) for c in m],
        ),
    )


def load_pricing(
    pricing_file: Path | None, url: str = PRICING_URL, cache: Path = PRICING_CACHE
) -> tuple[Pricing, str]:
    if pricing_file is not None:
        pricing = parse_pricing(
            pricing_file.read_text(encoding="utf-8"), str(pricing_file)
        )
        return pricing, f"from {pricing_file}"
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "claude-usage-report"}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            markdown = response.read().decode("utf-8", errors="replace")
        pricing = parse_pricing(markdown, url)
        if pricing.models:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(markdown, encoding="utf-8")
            except OSError:
                pass
            return pricing, f"fetched from {url} on {date.today().isoformat()}"
        problem = "the pricing page had no model price table I could read"
    except (OSError, ValueError) as exc:
        problem = f"could not fetch {url} ({exc})"
    try:
        fetched = date.fromtimestamp(cache.stat().st_mtime).isoformat()
        pricing = parse_pricing(
            cache.read_text(encoding="utf-8", errors="replace"), url
        )
    except OSError:
        return Pricing(), problem
    if pricing.models:
        return pricing, f"{problem}; using the copy saved on {fetched}"
    return Pricing(), problem


@dataclass
class Usage:
    input: int = 0
    cache_write: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0
    calls: int = 0
    cost_input: float = 0.0
    cost_write: float = 0.0
    cost_read: float = 0.0
    cost_output: float = 0.0
    cost_compare: float = 0.0
    unpriced: int = 0

    def add(self, other: Usage) -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    def input_total(self) -> int:
        return self.input + self.cache_write

    def cost(self) -> float:
        return self.cost_input + self.cost_write + self.cost_read + self.cost_output

    def as_dict(self) -> dict:
        return {
            "in": self.input_total(),
            "out": self.output,
            "read": self.cache_read,
            "calls": self.calls,
            "cost": round(self.cost(), 2),
            "cmp": round(self.cost_compare, 2),
            "c_in": round(self.cost_input, 2),
            "c_w": round(self.cost_write, 2),
            "c_r": round(self.cost_read, 2),
            "c_out": round(self.cost_output, 2),
            "unpriced": self.unpriced,
        }


def normalise_model(model: str) -> str:
    name = model.split("[", 1)[0].strip().lower()
    if "claude-" in name:
        name = name[name.index("claude-") :]
    name = re.sub(r"-v\d+(:\d+)?$", "", name)
    name = re.sub(r"[-@]\d{8}$", "", name)
    return MODEL_ALIASES.get(name, name)


def prices_for(model: str, prompt_tokens: int, pricing: Pricing):
    name = normalise_model(model)
    long_prompt = pricing.long_prompt.get(name)
    if long_prompt is not None and prompt_tokens > long_prompt[0]:
        return long_prompt[1]
    return pricing.models.get(name)


def price_usage(usage: Usage, prices) -> tuple[float, float, float, float]:
    base, write_5m, write_1h, read, output = prices
    one_hour = min(usage.cache_write_1h, usage.cache_write)
    return (
        usage.input * base / 1e6,
        ((usage.cache_write - one_hour) * write_5m + one_hour * write_1h) / 1e6,
        usage.cache_read * read / 1e6,
        usage.output * output / 1e6,
    )


def apply_costs(calls: list[Call], compare_model: str, pricing: Pricing) -> None:
    for call in calls:
        u = call.usage
        prompt = u.input + u.cache_write + u.cache_read
        prices = prices_for(call.model, prompt, pricing)
        if prices is None:
            u.unpriced = u.calls
        else:
            u.cost_input, u.cost_write, u.cost_read, u.cost_output = price_usage(
                u, prices
            )
        compare = prices_for(compare_model, prompt, pricing)
        if compare is not None:
            u.cost_compare = sum(price_usage(u, compare))


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
    creation = usage.get("cache_creation")
    values = Usage(
        input=usage.get("input_tokens") or 0,
        cache_write=usage.get("cache_creation_input_tokens") or 0,
        cache_write_1h=(
            (creation.get("ephemeral_1h_input_tokens") or 0)
            if isinstance(creation, dict)
            else 0
        ),
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
            for name in TOKEN_FIELDS:
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


def summarise(
    name: str, calls: list[Call], today: date, priced: bool = False, compare=""
) -> dict | None:
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

    if priced:
        best_cost_day = max(daily, key=lambda d: daily[d].cost())
        records["cost"] = [best_cost_day, round(daily[best_cost_day].cost(), 2)]

    this_month = months.get(today.isoformat()[:7], Usage())
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    scale = days_in_month / today.day
    month_now = {
        "ym": today.isoformat()[:7],
        "days": today.day,
        "days_in_month": days_in_month,
        "cost": round(this_month.cost(), 2),
        "cmp": round(this_month.cost_compare, 2),
        "projected": round(this_month.cost() * scale, 2),
        "projected_cmp": round(this_month.cost_compare * scale, 2),
    }

    week_cutoff = (today - timedelta(days=7)).isoformat()
    last7 = Usage()
    for day, usage in daily.items():
        if day > week_cutoff:
            last7.add(usage)

    return {
        "name": name,
        "priced": priced,
        "compare": compare,
        "month_now": month_now,
        "first_day": min(daily),
        "today": daily.get(today.isoformat(), Usage()).as_dict(),
        "last7": last7.as_dict(),
        "total": total.as_dict(),
        "records": records,
        "daily": [
            [
                d,
                daily[d].input_total(),
                daily[d].output,
                daily[d].cache_read,
                daily[d].calls,
                round(daily[d].cost(), 2),
            ]
            for d in sorted(daily)
        ],
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


def render(
    sections: list[dict],
    days: int,
    generated: str,
    budget: float | None = None,
    pricing_note: str = "",
) -> str:
    data = json.dumps(
        {
            "sections": sections,
            "days": days,
            "generated": generated,
            "budget": budget,
            "pricing_note": pricing_note,
        }
    )
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
function money(n) {
  if (n >= 100) return '$' + Math.round(n).toLocaleString('en-GB');
  return '$' + n.toFixed(2);
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
function tokenCells(u, priced) {
  const cost = priced ? `<td>${money(u.cost)}</td>` : '';
  return `<td>${fmt(u.in)}</td><td>${fmt(u.out)}</td><td>${fmt(u.read)}</td><td>${u.calls.toLocaleString('en-GB')}</td>${cost}`;
}
function tableHead(first, priced) {
  return `<tr><th>${first}</th><th>Input</th><th>Output</th><th>Cache read</th><th>API calls</th>${priced ? '<th>API cost</th>' : ''}</tr>`;
}
function renderCost(root, s) {
  const m = s.month_now;
  root.appendChild(el('h3', {}, 'What this would cost on the API'));
  root.appendChild(el('p', { class: 'note' },
    `Standard per-token API prices, ${text(DATA.pricing_note)}. Each call is priced at the model it used, with cache writes split into 5 minute and 1 hour.`
    + ' Excludes negotiated discounts, Bedrock or Vertex pricing, US-only inference and server tool fees.'));
  const items = [
    [money(m.cost), `${monthLabel(m.ym)} so far (${m.days} of ${m.days_in_month} days)`, '#ef9a9a'],
    [money(m.projected), `${monthLabel(m.ym)} projected`, '#ef9a9a'],
    [money(s.records.cost[1]), `Most expensive day<br>${dayLabel(s.records.cost[0])}`],
    [money(s.total.cost), 'All-time'],
  ];
  if (s.compare) items.splice(2, 0, [money(m.projected_cmp), `Projected if all on ${text(s.compare)}`]);
  if (DATA.budget) {
    const perDay = m.cost / m.days;
    const lasts = perDay > 0 ? Math.floor(DATA.budget / perDay) : null;
    items.push([lasts === null ? '-' : `${lasts} days`, `${money(DATA.budget)} budget lasts at this month's rate`]);
  }
  cards(root, items);
  if (s.total.unpriced) {
    root.appendChild(el('p', { class: 'note' },
      `${s.total.unpriced.toLocaleString('en-GB')} calls used models with no price on the pricing page and are left out of the cost.`));
  }

  const months = s.months.slice().reverse();
  const x = months.map(mm => mm.ym);
  const modelNames = [...new Set(s.months.flatMap(mm => Object.keys(mm.models)))].sort();
  const traces = modelNames.map((name, i) => ({
    x, y: months.map(mm => (mm.models[name] || { cost: 0 }).cost), name: text(name), type: 'bar',
    marker: { color: PALETTE[i % PALETTE.length] },
    hovertemplate: `<b>%{x|%B %Y}</b><br>${text(name)}: $%{y:,.0f}<extra></extra>`,
  }));
  if (s.compare) traces.push({
    x, y: months.map(mm => mm.cmp), name: `All on ${text(s.compare)}`, type: 'scatter', mode: 'lines+markers',
    line: { color: '#ffffff', width: 2, dash: 'dot' },
    hovertemplate: `<b>%{x|%B %Y}</b><br>All on ${text(s.compare)}: $%{y:,.0f}<extra></extra>`,
  });
  const shapes = DATA.budget ? [{ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: DATA.budget, y1: DATA.budget,
    line: { color: '#ef5350', width: 2, dash: 'dash' } }] : [];
  root.appendChild(el('h3', {}, 'API cost by month and model'));
  chart(root, traces, {
    barmode: 'stack', margin: { l: 70, r: 20, t: 10, b: 50 }, height: 360, shapes,
    xaxis: { gridcolor: '#2a3a5e', type: 'date', tickformat: '%b %Y', dtick: 'M1', tickangle: -45, automargin: true },
    yaxis: { gridcolor: '#2a3a5e', title: 'dollars', tickprefix: '$', separatethousands: true },
    legend: { orientation: 'h', y: -0.35, x: 0 },
  });

  root.appendChild(el('h3', {}, 'Where the cost goes'));
  chart(root, [
    ['c_r', 'Cache reads', C.read], ['c_w', 'Cache writes', '#ba68c8'], ['c_out', 'Output', C.out], ['c_in', 'Uncached input', C.in],
  ].map(([key, name, colour]) => ({
    x, y: months.map(mm => mm[key]), name, type: 'bar', marker: { color: colour },
    hovertemplate: `<b>%{x|%B %Y}</b><br>${name}: $%{y:,.0f}<extra></extra>`,
  })), {
    barmode: 'stack', margin: { l: 70, r: 20, t: 10, b: 50 }, shapes,
    xaxis: { gridcolor: '#2a3a5e', type: 'date', tickformat: '%b %Y', dtick: 'M1', tickangle: -45, automargin: true },
    yaxis: { gridcolor: '#2a3a5e', title: 'dollars', tickprefix: '$', separatethousands: true },
  });
}

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

  if (s.priced) renderCost(root, s);

  const recent = s.daily.slice(-DATA.days).map(([day, i, o, r, calls, cost]) => ({ day, in: i, out: o, read: r, calls, cost }));
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
  const table = el('table', {}, tableHead('Month', s.priced));
  s.months.forEach(m => {
    const id = `${s.name}-${m.ym}`.replace(/[^A-Za-z0-9-]/g, '');
    const row = el('tr', { class: 'month' }, `<td>${monthLabel(m.ym)} <span class="arrow" id="a-${id}">&#9654;</span></td>${tokenCells(m, s.priced)}`);
    row.addEventListener('click', () => {
      const subs = table.querySelectorAll(`.s-${id}`);
      const show = subs.length && subs[0].style.display === 'none';
      subs.forEach(r => { r.style.display = show ? '' : 'none'; });
      document.getElementById(`a-${id}`).classList.toggle('open', show);
    });
    table.appendChild(row);
    m.weeks.forEach(w => {
      table.appendChild(el('tr', { class: `sub s-${id}`, style: 'display:none' }, `<td>Week ${w.week}</td>${tokenCells(w, s.priced)}`));
    });
    Object.entries(m.models).sort((a, b) => b[1].out - a[1].out).forEach(([name, u]) => {
      table.appendChild(el('tr', { class: `sub s-${id}`, style: 'display:none' }, `<td>${text(name)}</td>${tokenCells(u, s.priced)}`));
    });
  });
  wrap.appendChild(table);
  root.appendChild(wrap);

  root.appendChild(el('h3', {}, 'Daily breakdown'));
  const dwrap = el('div', { class: 'table-wrap' });
  const dtable = el('table', {}, tableHead('Date', s.priced));
  recent.slice().reverse().forEach(r => {
    dtable.appendChild(el('tr', {}, `<td>${dayLabel(r.day)}</td>${tokenCells(r, s.priced)}`));
  });
  dwrap.appendChild(dtable);
  root.appendChild(dwrap);
}

const root = document.getElementById('root');
root.appendChild(el('h1', {}, 'Token Usage Report'));
root.appendChild(el('p', { class: 'note' },
  'Built from local Claude Code and Codex transcripts. Input includes cache writes. Each API call is counted once.'));
if (DATA.sections.length && !DATA.sections.some(s => s.priced) && DATA.pricing_note) {
  root.appendChild(el('p', { class: 'note' }, `No API cost estimate: ${text(DATA.pricing_note)}.`));
}
if (!DATA.sections.length) root.appendChild(el('p', {}, 'No Claude Code or Codex transcripts were found.'));
DATA.sections.forEach(s => renderSection(root, s));
root.appendChild(el('div', { class: 'footer' }, `Generated ${text(DATA.generated)}`));
</script></body></html>
"""


def positive_amount(value: str) -> float:
    try:
        amount = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value}") from None
    if not math.isfinite(amount) or amount <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive amount: {value}")
    return amount


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
        "--compare-model",
        help="also price every Claude call as if it ran on this model "
        "(default: the cheapest Opus on the pricing page)",
    )
    parser.add_argument(
        "--pricing-file",
        type=Path,
        help=f"read prices from a saved copy of {PRICING_URL} instead of fetching it",
    )
    parser.add_argument(
        "--no-cost", action="store_true", help="leave out the API cost estimate"
    )
    parser.add_argument(
        "--budget",
        type=positive_amount,
        help="monthly API budget in dollars, drawn on the cost chart",
    )
    parser.add_argument(
        "--open", action="store_true", help="open the report in a browser when done"
    )
    args = parser.parse_args(argv)
    today = date.today()
    sections = []
    claude_calls = scan_claude(args.claude_dir)
    priced = False
    compare = ""
    pricing_note = "API cost estimate turned off."
    if not args.no_cost:
        pricing, pricing_note = load_pricing(args.pricing_file)
        if not pricing.models:
            print(f"warning: no API cost estimate: {pricing_note}", file=sys.stderr)
        else:
            compare = args.compare_model or default_compare_model(pricing) or ""
            if args.compare_model and prices_for(compare, 0, pricing) is None:
                parser.error(
                    f"no price for {compare} on the pricing page; choose one of: "
                    + ", ".join(sorted(pricing.models))
                )
            apply_costs(claude_calls, compare, pricing)
            priced = True
    claude = summarise(
        "Claude Code", claude_calls, today, priced=priced, compare=compare
    )
    if claude:
        sections.append(claude)
    if not args.no_codex:
        codex = summarise("Codex", scan_codex(args.codex_dir), today)
        if codex:
            sections.append(codex)

    output = Path(args.output)
    output.write_text(
        render(
            sections,
            args.days,
            datetime.now().strftime("%d %b %Y %H:%M"),
            args.budget,
            pricing_note,
        ),
        encoding="utf-8",
    )
    for s in sections:
        if s["priced"]:
            m = s["month_now"]
            print(
                f"{s['name']}: API cost this month so far ${m['cost']:,.0f}, "
                f"projected ${m['projected']:,.0f}"
                + (
                    f" (all on {s['compare']}: ${m['projected_cmp']:,.0f})"
                    if s["compare"]
                    else ""
                )
            )
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
