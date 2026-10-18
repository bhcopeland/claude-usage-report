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
        self.assertEqual(s["total"], {"in": 54, "out": 39, "read": 1050, "calls": 3})
        self.assertEqual(s["today"]["out"], 30)
        self.assertEqual(s["last7"]["calls"], 3)
        aug = s["months"][0]
        self.assertEqual(aug["ym"], "2025-08")
        self.assertEqual([w["week"] for w in aug["weeks"]], [32, 31])
        self.assertEqual(set(aug["models"]), {"a", "b"})
        self.assertEqual(s["daily"][0], ["2025-07-30", 11, 7, 100, 1])

    def test_no_calls_gives_no_section(self):
        self.assertIsNone(r.summarise("Codex", [], date(2026, 1, 1)))


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
