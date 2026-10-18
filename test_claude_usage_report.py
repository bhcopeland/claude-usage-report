import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import claude_usage_report as r


def claude_line(msg_id, ts, output, model="claude-opus-5-5", request="req"):
    message = {
        "model": model,
        "usage": {
            "input_tokens": 3,
            "output_tokens": output,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20,
        },
    }
    if msg_id:
        message["id"] = msg_id
    return json.dumps({"timestamp": ts, "requestId": request, "message": message})


class ClaudeScanTests(unittest.TestCase):
    def scan(self, lines):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "project" / "session" / "subagents"
            sub.mkdir(parents=True)
            (sub / "agent.jsonl").write_text("\n".join(lines) + "\n")
            return r.scan_claude(Path(tmp))

    def test_repeated_lines_for_one_call_count_once_at_their_maximum(self):
        calls = self.scan(
            [
                claude_line("msg_a", "2026-10-18T10:00:00", 1),
                claude_line("msg_a", "2026-10-18T10:00:00", 250),
                claude_line("msg_a", "2026-10-18T10:00:00", 40),
                claude_line("msg_b", "2026-10-18T11:00:00", 10),
            ]
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(sorted(c.usage.output for c in calls), [10, 250])

    def test_lines_without_an_id_each_count_and_synthetic_is_skipped(self):
        calls = self.scan(
            [
                claude_line(None, "2026-10-18T10:00:00", 5),
                claude_line(None, "2026-10-18T10:00:00", 5),
                claude_line("x", "2026-10-18T10:00:00", 9, model="<synthetic>"),
                '{"type":"user","message":"input_tokens"}',
                "not json input_tokens",
            ]
        )
        self.assertEqual([c.usage.output for c in calls], [5, 5])

    def test_missing_directory_gives_no_calls(self):
        self.assertEqual(r.scan_claude(Path("/nonexistent/claude-usage-report")), [])


class CodexScanTests(unittest.TestCase):
    def test_model_comes_from_turn_context_and_responses_dedupe(self):
        def usage(rid, inp, cached, out):
            return json.dumps(
                {
                    "timestamp": "2026-10-18T09:00:00",
                    "type": "token_usage_record",
                    "payload": {
                        "response_id": rid,
                        "usage": {
                            "input_tokens": inp,
                            "cached_input_tokens": cached,
                            "cache_write_input_tokens": 7,
                            "output_tokens": out,
                        },
                    },
                }
            )

        lines = [
            usage("r0", 50, 10, 1),
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.5"}}),
            usage("r1", 100, 40, 9),
            usage("r1", 100, 40, 9),
        ]
        calls = r.scan_codex_lines(lines, set())
        self.assertEqual([c.model for c in calls], ["unknown", "gpt-5.5"])
        second = calls[1].usage
        self.assertEqual(
            (second.input, second.cache_read, second.cache_write, second.output),
            (60, 40, 7, 9),
        )


class SummaryTests(unittest.TestCase):
    def call(self, day, model, inp, out, read):
        return r.Call(
            day,
            model,
            r.Usage(input=inp, cache_write=1, cache_read=read, output=out, calls=1),
        )

    def test_records_totals_months_weeks_and_models(self):
        calls = [
            self.call("2025-07-30", "a", 10, 7, 100),
            self.call("2025-08-01", "a", 40, 2, 900),
            self.call("2025-08-04", "b", 1, 30, 50),
        ]
        s = r.summarise("Claude Code", calls, date(2025, 8, 4))
        self.assertEqual(s["first_day"], "2025-07-30")
        self.assertEqual(s["records"]["in"], ["2025-08-01", 41])
        self.assertEqual(s["records"]["out"], ["2025-08-04", 30])
        self.assertEqual(s["records"]["read"], ["2025-08-01", 900])
        self.assertEqual(
            {k: s["total"][k] for k in ("in", "out", "read", "calls")},
            {"in": 54, "out": 39, "read": 1050, "calls": 3},
        )
        self.assertEqual(s["today"]["out"], 30)
        self.assertEqual(s["last7"]["calls"], 3)
        aug = s["months"][0]
        self.assertEqual(aug["ym"], "2025-08")
        self.assertEqual([w["week"] for w in aug["weeks"]], [32, 31])
        self.assertEqual(set(aug["models"]), {"a", "b"})
        self.assertEqual(s["daily"][0], ["2025-07-30", 11, 7, 100, 1, 0.0])

    def test_no_calls_gives_no_section(self):
        self.assertIsNone(r.summarise("Codex", [], date(2026, 1, 1)))


PAGE = """
# Pricing

| Model | Base input tokens | 5m cache writes | 1h cache writes | Cache hits and refreshes | Output tokens |
| --- | --- | --- | --- | --- | --- |
| Claude Opus 6 | $3 / MTok | $3.75 / MTok | $6 / MTok | $0.15 / MTok<sup>1</sup> | $15 / MTok |
| Claude Opus 5.5 | $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok<sup>2</sup> | $20 / MTok |
| Claude Opus 4.1 ([retired](https://example.com)) | $15 / MTok | $18.75 / MTok | $30 / MTok | $1.50 / MTok | $75 / MTok |
| Claude Haiku 5.5 (for prompts up to 100,000 tokens) | $0.10 / MTok | $0.125 / MTok | $0.20 / MTok | $0.01 / MTok | $0.50 / MTok |
| Claude Haiku 5.5 (for prompts over 100,000 tokens) | $0.50 / MTok | $0.625 / MTok | $1 / MTok | $0.05 / MTok | $2.50 / MTok |

| Model | Batch input | Batch output |
| --- | --- | --- |
| Claude Opus 6 | $1.50 / MTok | $7.50 / MTok |
"""


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.pricing = r.parse_pricing(PAGE, "test")

    def test_new_models_are_read_from_the_page_without_code_changes(self):
        self.assertEqual(
            self.pricing.models["claude-opus-6"], (3.0, 3.75, 6.0, 0.15, 15.0)
        )
        self.assertEqual(self.pricing.models["claude-opus-4-1"][0], 15.0)
        self.assertEqual(
            sorted(self.pricing.models),
            ["claude-haiku-5-5", "claude-opus-4-1", "claude-opus-5-5", "claude-opus-6"],
        )

    def test_transcript_model_ids_map_onto_page_names(self):
        for model, expected in (
            ("claude-opus-6", 3.0),
            ("claude-opus-4-1-20250805", 15.0),
            ("us.anthropic.claude-opus-4-1-20250805-v1:0", 15.0),
            ("claude-opus-5-5[1m]", 4.0),
        ):
            self.assertEqual(r.prices_for(model, 0, self.pricing)[0], expected, model)
        self.assertIsNone(r.prices_for("claude-unknown-9", 0, self.pricing))

    def test_long_prompt_tier_applies_above_its_threshold(self):
        self.assertEqual(
            r.prices_for("claude-haiku-5-5", 100_000, self.pricing)[0], 0.1
        )
        self.assertEqual(
            r.prices_for("claude-haiku-5-5", 100_001, self.pricing)[0], 0.5
        )

    def test_default_comparison_is_the_cheapest_opus_on_the_page(self):
        self.assertEqual(r.default_compare_model(self.pricing), "claude-opus-6")
        self.assertIsNone(r.default_compare_model(r.Pricing()))

    def test_costs_split_cache_writes_and_flag_unpriced_models(self):
        priced = r.Call(
            "2026-10-01",
            "claude-opus-5-5",
            r.Usage(
                input=1_000_000,
                cache_write=3_000_000,
                cache_write_1h=1_000_000,
                cache_read=10_000_000,
                output=1_000_000,
                calls=1,
            ),
        )
        unknown = r.Call("2026-10-01", "claude-unknown-9", r.Usage(input=5, calls=1))
        r.apply_costs([priced, unknown], "claude-opus-6", self.pricing)
        u = priced.usage
        self.assertAlmostEqual(u.cost_input, 4.0)
        self.assertAlmostEqual(u.cost_write, 2 * 5.0 + 1 * 8.0)
        self.assertAlmostEqual(u.cost_read, 2.0)
        self.assertAlmostEqual(u.cost_output, 20.0)
        self.assertAlmostEqual(u.cost_compare, 3.0 + 2 * 3.75 + 6.0 + 1.5 + 15.0)
        self.assertEqual(unknown.usage.unpriced, 1)
        self.assertEqual(unknown.usage.cost(), 0)

    def test_falls_back_to_the_saved_copy_when_the_fetch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "pricing.md"
            cache.write_text(PAGE)
            pricing, note = r.load_pricing(
                None, url="http://127.0.0.1:9/none", cache=cache
            )
            self.assertIn("claude-opus-6", pricing.models)
            self.assertIn("using the copy saved", note)
            cache.unlink()
            pricing, note = r.load_pricing(
                None, url="http://127.0.0.1:9/none", cache=cache
            )
            self.assertEqual(pricing.models, {})
            self.assertIn("could not fetch", note)

    def test_linked_model_names_are_still_read(self):
        page = PAGE.replace(
            "| Claude Haiku 5.5 (for prompts over",
            "| [Claude Haiku 5.5](https://example.com/haiku) (for prompts over",
        )
        pricing = r.parse_pricing(page, "test")
        self.assertEqual(pricing.long_prompt["claude-haiku-5-5"][0], 100_000)

    def test_unreadable_saved_copy_gives_no_prices_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "pricing.md"
            cache.mkdir()
            pricing, note = r.load_pricing(
                None, url="http://127.0.0.1:9/none", cache=cache
            )
            self.assertEqual(pricing.models, {})
            self.assertIn("could not fetch", note)

    def test_budget_must_be_positive_and_finite(self):
        self.assertEqual(r.positive_amount("1000"), 1000.0)
        for bad in ("nan", "inf", "1e309", "0", "-5", "lots"):
            with self.assertRaises(r.argparse.ArgumentTypeError, msg=bad):
                r.positive_amount(bad)

    def test_unreadable_page_gives_no_prices(self):
        self.assertEqual(r.parse_pricing("# nothing here", "x").models, {})


class RenderTests(unittest.TestCase):
    def embedded(self, html):
        start = html.index('id="report-data">') + len('id="report-data">')
        return html[start : html.index("</script>", start)]

    def test_transcript_strings_cannot_break_out_of_the_data_block(self):
        name = "x<!--<script></script><b>&"
        html = r.render([{"name": name, "models": {"__proto__": 1}}], 30, "now")
        blob = self.embedded(html)
        self.assertNotIn("<", blob)
        self.assertNotIn(">", blob)
        self.assertNotIn("&", blob)
        data = json.loads(blob)
        self.assertEqual(data["sections"][0]["name"], name)
        self.assertEqual(data["sections"][0]["models"], {"__proto__": 1})


if __name__ == "__main__":
    unittest.main()
