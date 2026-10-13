import _path  # noqa: F401
import contextlib
import io
import json
import re
import sys
import unittest
from unittest import mock

from claude_cache_audit import cli, report

from helpers import BASE, Env, SESSION, asst, boundary, idle_marker, pair, user


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class TestCli(Env):
    def populate(self):
        self.main_file([user(0)] + pair(gap=600, prev_stop="tool_use", prev_tools=("Bash",)) + [
            boundary(700, "auto"), idle_marker(701), asst(5000, "m3", cw=1000, cw5m=1000)])
        self.sub_file([asst(10, "s1", cw=1000, cw5m=1000)], meta={"agentType": "implementer"})

    def args(self, *more):
        return ["--config-dir", self.config, "--all"] + list(more)

    def test_no_transcripts_exits_1_and_lists_dirs(self):
        code, out, err = run(["--config-dir", self.root + "/nowhere"])
        self.assertEqual(code, 1)
        self.assertIn("nowhere", err)
        self.assertEqual(out, "")

    def test_text_report_sections_and_width(self):
        self.populate()
        code, out, _ = run(self.args())
        self.assertEqual(code, 0)
        for s in ("Usage split", "By agent kind", "Subagents by peak context", "Cache rebuilds", "hit rate", "Context size",
                  "Idle compactions", "1-hour subagent cache", "API-equivalent prices as of 2026-10-13",
                  "malformed lines skipped: 0"):
            self.assertIn(s, out)
        self.assertLessEqual(max(len(x) for x in out.splitlines()), 100)

    def test_json_has_stable_keys(self):
        self.populate()
        code, out, _ = run(self.args("--json"))
        d = json.loads(out)
        self.assertEqual(d["schema_version"], 1)
        self.assertEqual(list(d), ["schema_version", "tool_version", "pricing", "window", "totals", "usage_split",
                                   "by_kind", "agent_peak_context", "model_mix", "rebuilds", "hit_rate", "context_size", "compactions",
                                   "idle_compactions", "one_hour_subagent_cache", "try_this"])
        self.assertEqual(list(d["usage_split"]), ["cache_read", "cache_write_5m", "cache_write_1h",
                                                  "uncached_input", "output"])
        self.assertEqual(list(d["by_kind"]), ["main", "subagent", "teammate", "fork"])
        self.assertEqual(list(d["rebuilds"]), ["cold_starts", "post_rule", "headline"])
        self.assertEqual(list(d["rebuilds"]["headline"]["by_cause"]),
                         ["compaction", "model_switch", "expired", "prefix_change"])
        self.assertEqual(list(d["one_hour_subagent_cache"]), ["subagent", "teammate", "fork", "all_subagents"])
        self.assertEqual(d["pricing"]["as_of"], "2026-10-13")

    def test_share_is_safe_to_paste(self):
        self.populate()
        code, out, _ = run(self.args("--share"))
        self.assertEqual(code, 0)
        for bad in ("$", "/", "~"):
            self.assertNotIn(bad, out)
        self.assertIsNone(re.search(r"[0-9a-fA-F]{8}", out), out)
        for bad in ("secretproj", SESSION[:8], "someone", "SECRET"):
            self.assertNotIn(bad, out)
        self.assertIn("Cost split", out)

    def test_share_shows_peak_context_as_ratios_only(self):
        self.sub_file([asst(0, "s1", cr=600000)], agent="agent-big")
        self.sub_file([asst(1, "s2", cr=1000)], agent="agent-small")
        code, out, _ = run(self.args("--share"))
        self.assertEqual(code, 0)
        lines = [x for x in out.splitlines() if "by peak context" in x]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn(">=500K", line)
            self.assertNotIn("$", line)
        self.assertIn("<100K 50.0%", lines[0])  # a share of agents, not a count
        self.assertNotIn("$", out)

    def test_text_report_peak_context_section(self):
        self.sub_file([asst(0, "s1", cr=600000)], agent="agent-big")
        code, out, _ = run(self.args())
        self.assertEqual(code, 0)
        self.assertIn("3. Subagents by peak context", out)
        self.assertIn("4. Cache rebuilds", out)
        self.assertIn("passed 500K", out)

    def test_share_hides_unknown_model_ids(self):
        arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abcd1234ef"
        self.main_file([
            asst(0, "m1", model="claude-3-5-haiku-20241022", cw=1000, cw5m=1000),
            asst(10, "m2", model="claude-sonnet-4-20250514", cw=1000, cw5m=1000),
            asst(20, "m3", model=arn, cw=1000, cw5m=1000),
            asst(30, "m4", model="anthropic/claude-weird-9", cw=1000, cw5m=1000)])
        code, out, _ = run(self.args("--share"))
        self.assertEqual(code, 0)
        mix = next(x for x in out.splitlines() if x.startswith("- Model mix"))
        for bad in ("/", "arn", "20241022", "20250514", "bedrock", "123456789012"):
            self.assertNotIn(bad, out)
        self.assertNotIn(":", mix.split(": ", 1)[1])
        self.assertIn("other", mix)
        self.assertIn("3-5-haiku", mix)
        self.assertIn("sonnet-4", mix)

    def test_no_prompt_text_in_any_output(self):
        self.populate()
        for extra in ([], ["--json"], ["--share"], ["rebuilds"]):
            code, out, err = run(self.args(*extra))
            self.assertEqual(code, 0)
            for secret in ("SECRETPROMPT", "SECRETCMD", "rm -rf"):
                self.assertNotIn(secret, out + err)

    def test_rebuilds_command(self):
        self.populate()
        code, out, _ = run(self.args("rebuilds", "--top", "1"))
        self.assertEqual(code, 0)
        lines = [x for x in out.splitlines() if x.strip()]
        self.assertIn("secretproj", lines[1])
        self.assertIn(SESSION[:8], lines[1])
        self.assertLessEqual(max(len(x) for x in lines), 100)
        self.assertEqual(len(lines), 3)  # header, one rebuild, footer

    def test_rebuilds_lines_fit_100_columns_with_long_cells(self):
        long_cwd = "/home/someone/a-very-long-project-folder-name"
        a = asst(0, "m1", cw=100000, cw5m=100000, stop="tool_use", tools=("mcp__averyverylongserver__tool",),
                 cwd=long_cwd)
        b = asst(40 * 3600, "m2", cw=100000, cw5m=100000, inp=1, cwd=long_cwd)
        self.main_file([a, b])
        self.sub_file([asst(1, "s1", cw=1000, cw5m=1000, cwd=long_cwd),
                       asst(2000, "s2", cw=900000, cw5m=900000, inp=1, cwd=long_cwd)],
                      meta={"agentType": "a-subagent-type-with-a-long-name"})
        code, out, _ = run(self.args("rebuilds"))
        self.assertEqual(code, 0)
        lines = [x for x in out.splitlines() if x.strip()]
        self.assertGreaterEqual(len(lines), 4)
        self.assertLessEqual(max(len(x) for x in lines), 100)

    def test_top_tools_line_wraps_between_items_only(self):
        names = ["VeryLongToolNameNumber%d" % i for i in range(5)]
        for i, name in enumerate(names):
            sid = "%08d-4567-89ef-0123-456789abcdef" % i
            self.sub_file([asst(1, "s1%d" % i, cw=1000, cw5m=1000, stop="tool_use", tools=(name,), session=sid),
                           asst(900, "s2%d" % i, cw=100000, cw5m=100000, inp=1, session=sid)],
                          agent="agent-b%d" % i, session=sid, meta={"agentType": "implementer"})
        code, out, _ = run(self.args())
        self.assertEqual(code, 0)
        lines = out.splitlines()
        start = next(i for i, x in enumerate(lines) if "Top tools subagents waited on" in x)
        block = [lines[start]]
        for x in lines[start + 1:]:
            if not x.startswith("    "):
                break
            block.append(x)
        self.assertGreater(len(block), 1)
        self.assertLessEqual(max(len(x) for x in block), 100)
        for x in block[:-1]:
            self.assertTrue(x.endswith(","), x)
        self.assertEqual(len(re.findall(r"VeryLongToolNameNumber\d \d+ \(", " ".join(block))), 5)

    def test_mcp_tool_names_collapse_to_mcp(self):
        self.main_file([asst(0, "m1", cw=100000, cw5m=100000, stop="tool_use", tools=("mcp__acmecorp__lookup",)),
                        asst(900, "m2", cw=100000, cw5m=100000, inp=1)])
        self.sub_file([asst(1, "s1", cw=1000, cw5m=1000, stop="tool_use", tools=("mcp__acmecorp__lookup",)),
                       asst(900, "s2", cw=100000, cw5m=100000, inp=1)], meta={"agentType": "implementer"})
        for extra in ([], ["--share"], ["--json"], ["rebuilds"]):
            code, out, _ = run(self.args(*extra))
            self.assertEqual(code, 0)
            self.assertNotIn("acmecorp", out)
            self.assertNotIn("mcp__", out)
        code, out, _ = run(self.args())
        self.assertIn("mcp", out)

    def test_rebuilds_options_after_subcommand(self):
        self.populate()
        code, out, _ = run(["rebuilds", "--config-dir", self.config, "--all", "--top", "5"])
        self.assertEqual(code, 0)
        self.assertIn("secretproj", out)

    def test_version_and_help(self):
        code, out, _ = run(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("0.1.0", out)
        code, out, _ = run(["--help"])
        self.assertIn("--share", out)

    def test_explicit_window_outside_the_data_is_empty(self):
        # fixture data is from 2026-09; a window ending far in the past should be empty
        self.populate()
        code, out, _ = run(["--config-dir", self.config, "--since", "2020-01-01", "--until", "2020-01-02"])
        self.assertEqual(code, 0)
        self.assertIn("No requests in this window", out)

    def test_default_window_without_options_uses_the_last_30_days(self):
        self.populate()  # requests at +0, +10, +600 and +5000 seconds from BASE
        base = ["--config-dir", self.config, "--json"]
        for now, expected in ((BASE + 30 * 86400 - 3600, 4), (BASE + 30 * 86400 + 1800, 1)):
            with mock.patch.object(cli.time, "time", return_value=now):
                code, out, _ = run(base)
            self.assertEqual(code, 0)
            d = json.loads(out)
            self.assertEqual(d["totals"]["requests"], expected)
            self.assertAlmostEqual(d["window"]["days"], 30.0, places=1)
        with mock.patch.object(cli.time, "time", return_value=BASE + 40 * 86400):
            code, out, _ = run(["--config-dir", self.config])
        self.assertIn("No requests in this window", out)

    def test_parse_when(self):
        now = 1_800_000_000
        self.assertEqual(cli.parse_when("7d", now), now - 7 * 86400)
        self.assertEqual(cli.parse_when("12h", now), now - 12 * 3600)
        a = cli.parse_when("2026-10-12", now)
        b = cli.parse_when("2026-10-12", now, is_until=True)
        self.assertEqual(b - a, 86400)
        self.assertEqual(cli.parse_when("2026-10-12T15:00", now) - a, 15 * 3600)
        with self.assertRaises(Exception):
            cli.parse_when("last tuesday", now)


if __name__ == "__main__":
    unittest.main()
