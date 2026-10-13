import os
import _path  # noqa: F401
import unittest

from claude_cache_audit import transcripts

from helpers import Env, asst, boundary, idle_marker, user


class TestParsing(Env):
    def test_duplicate_lines_count_once_and_merge_tools(self):
        line = asst(0, "m1", cw=1000, cw5m=1000, stop=None, tools=("Read",))
        later = asst(1, "m1", cw=1000, cw5m=1000, stop="tool_use", tools=("Bash",))
        self.main_file([line, line, later])
        scan = self.scan()
        self.assertEqual(len(scan.requests), 1)
        self.assertEqual(scan.requests[0].tools, ["Read", "Read", "Bash"])
        self.assertEqual(scan.requests[0].stop, "tool_use")

    def test_dedupe_across_files_prefers_subagent(self):
        shared = asst(0, "m1", cw=500, cw5m=500)
        self.main_file([shared, asst(10, "m2", cw=500, cw5m=500)])
        self.sub_file([shared])
        scan = self.scan()
        self.assertEqual(len(scan.requests), 2)
        by_mid = {r.mid: r for r in scan.requests}
        self.assertTrue(by_mid["m1"].is_sub)
        self.assertFalse(by_mid["m2"].is_sub)

    def test_symlinked_config_dir_counts_once(self):
        self.main_file([asst(0, "m1", cw=500, cw5m=500)])
        link = os.path.join(self.root, "claude-link")
        os.symlink(self.config, link)
        files = transcripts.find_files([self.config, link])
        self.assertEqual(len(files), 1)
        scan = transcripts.scan_transcripts([self.config, link])
        self.assertEqual(len(scan.requests), 1)
        self.assertEqual(transcripts.dedupe_dirs([self.config, link]), [self.config])

    def test_symlinked_file_counts_once(self):
        p = self.main_file([asst(0, "m1", cw=500, cw5m=500)])
        os.symlink(p, self.proj("other-session.jsonl"))
        self.assertEqual(len(self.scan().requests), 1)

    def test_malformed_lines_are_skipped_and_counted(self):
        self.main_file([asst(0, "m1", cw=500, cw5m=500), '{"type": "assistant", broken', "not json at all",
                        asst(5, "m2", cw=500, cw5m=500)])
        scan = self.scan()
        self.assertEqual(len(scan.requests), 2)
        self.assertEqual(scan.malformed, 1)  # only lines that pass the cheap prefilter are parsed

    def test_synthetic_model_is_ignored(self):
        self.main_file([asst(0, "m1", model="<synthetic>", cw=500, cw5m=500), asst(1, "m2", cw=1, cw5m=1)])
        self.assertEqual(len(self.scan().requests), 1)

    def test_iterations_replace_smaller_top_level_usage(self):
        e = asst(0, "m1", inp=1, cr=10, cw=0, out=5)
        e["message"]["usage"]["iterations"] = [
            {"input_tokens": 1, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 1000,
             "output_tokens": 5, "cache_creation": {"ephemeral_5m_input_tokens": 1000}}]
        self.main_file([e])
        r = self.scan().requests[0]
        self.assertEqual((r.cw, r.cw5m), (1000, 1000))

    def test_agent_kinds_from_meta(self):
        self.main_file([asst(0, "m0", cw=1, cw5m=1)])
        self.sub_file([asst(1, "s1", cw=1, cw5m=1)], agent="agent-fork", meta={"agentType": "fork"})
        self.sub_file([asst(2, "s2", cw=1, cw5m=1)], agent="agent-mate",
                      meta={"agentType": "reviewer", "taskKind": "in_process_teammate"})
        self.sub_file([asst(3, "s3", cw=1, cw5m=1)], agent="agent-plain", meta={"agentType": "implementer"})
        rep, _ = self.compute()
        kinds = {k: v["requests"] for k, v in rep["by_kind"].items()}
        self.assertEqual(kinds, {"main": 1, "subagent": 1, "teammate": 1, "fork": 1})

    def test_prompt_text_is_not_kept(self):
        self.main_file([user(0), asst(1, "m1", cw=500, cw5m=500, tools=("Bash",))])
        r = self.scan().requests[0]
        blob = repr({s: getattr(r, s, None) for s in r.__slots__ if hasattr(r, s)})
        self.assertNotIn("SECRET", blob)

    def test_compaction_kinds(self):
        self.main_file([
            asst(0, "m1", cw=500, cw5m=500),
            boundary(100, "auto"),
            boundary(200, "manual"),
            boundary(300, "auto", pre=300000, post=20000), idle_marker(301),
            boundary(400, "manual", pre=250000, post=15000), idle_marker(401),
        ])
        kinds = sorted(c.kind for c in self.scan().compactions)
        self.assertEqual(kinds, ["auto", "idle", "idle", "manual"])

    def test_idle_marker_without_boundary_still_counts(self):
        self.main_file([asst(0, "m1", cw=500, cw5m=500), idle_marker(50)])
        cs = self.scan().compactions
        self.assertEqual([c.kind for c in cs], ["idle"])
        self.assertIsNone(cs[0].pre)

    def test_scan_skips_files_older_than_window(self):
        p = self.main_file([asst(0, "m1", cw=500, cw5m=500)])
        os.utime(p, (1_000_000_000, 1_000_000_000))
        scan = self.scan(since=1_700_000_000)
        self.assertEqual(len(scan.requests), 0)
        self.assertEqual(scan.skipped_old, 1)

    def test_parse_ts_variants(self):
        self.assertIsNotNone(transcripts.parse_ts("2026-09-01T12:00:00.5Z"))
        self.assertIsNotNone(transcripts.parse_ts("2026-09-01T12:00:00Z"))
        self.assertIsNone(transcripts.parse_ts("garbage"))


if __name__ == "__main__":
    unittest.main()
