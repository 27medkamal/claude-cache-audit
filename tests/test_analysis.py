import _path  # noqa: F401
import unittest

from claude_cache_audit import analysis, pricing

from helpers import BASE, Env, asst, boundary, idle_marker, pair


class TestPricing(Env):
    def test_5m_and_1h_write_prices(self):
        self.main_file([asst(0, "m1", cw=1_000_000, cw5m=1_000_000)])
        rep, _ = self.compute()
        self.assertAlmostEqual(rep["usage_split"]["cache_write_5m"]["cost"], 6.25, places=2)
        self.assertEqual(rep["usage_split"]["cache_write_1h"]["cost"], 0)
        self.main_file([asst(0, "m1", cw=1_000_000, cw1h=1_000_000)])
        rep, _ = self.compute()
        self.assertAlmostEqual(rep["usage_split"]["cache_write_1h"]["cost"], 10.0, places=2)

    def test_write_without_ttl_split_is_priced_as_5m_and_reported(self):
        self.main_file([asst(0, "m1", cw=1_000_000)])
        rep, _ = self.compute()
        self.assertAlmostEqual(rep["usage_split"]["cache_write_5m"]["cost"], 6.25, places=2)
        self.assertEqual(rep["totals"]["write_tokens_without_ttl_split"], 1_000_000)

    def test_unknown_model_is_free_and_listed(self):
        self.main_file([asst(0, "m1", model="claude-zebra-9", inp=1_000_000, out=1_000_000),
                        asst(1, "m2", inp=1_000_000)])
        rep, _ = self.compute()
        self.assertEqual(rep["totals"]["unknown_models"], {"claude-zebra-9": 1})
        self.assertAlmostEqual(rep["totals"]["cost"], 5.0, places=2)  # only the known request

    def test_fast_mode_and_haiku_long_tier(self):
        self.assertEqual(pricing.price("claude-opus-5", 0, "fast"), (10, 50, 1.0))
        self.assertEqual(pricing.price("claude-opus-5-5", 0, "fast"), (8, 40, 0.4))
        self.assertEqual(pricing.price("claude-opus-4-8", 0, "fast"), (10, 50, 1.0))
        for m in ("claude-opus-4-6", "claude-opus-4-7", "claude-sonnet-5", "claude-haiku-5-5", "claude-fable-5"):
            self.assertEqual(pricing.price(m, 0, "fast"), pricing.price(m, 0, None), m)
        self.assertEqual(pricing.price("claude-opus-4-0", 0, None), (15, 75, 1.5))
        self.assertEqual(pricing.price("claude-opus-4-20250514", 0, None), (15, 75, 1.5))
        self.assertEqual(pricing.price("claude-opus-4-5", 0, None), (5, 25, 0.5))
        self.assertEqual(pricing.price("claude-haiku-5-5", 100_001, None), (0.5, 2.5, 0.05))
        self.assertEqual(pricing.price("claude-haiku-5-5", 100_000, None), (0.10, 0.50, 0.01))
        self.assertIsNone(pricing.price("claude-unknown", 0, None))

    def test_thinking_share(self):
        self.main_file([asst(0, "m1", out=100, think=40)])
        rep, _ = self.compute()
        self.assertEqual(rep["usage_split"]["output"]["thinking_pct_of_output_tokens"], 40.0)


class TestWindow(Env):
    def test_window_filter(self):
        self.main_file([asst(0, "m1", cw=10, cw5m=10), asst(86400, "m2", cw=10, cw5m=10),
                        asst(2 * 86400, "m3", cw=10, cw5m=10)])
        rep, _ = self.compute(since=BASE + 3600, until=BASE + 86400 + 3600)
        self.assertEqual(rep["totals"]["requests"], 1)
        rep, _ = self.compute()
        self.assertEqual(rep["totals"]["requests"], 3)

    def test_first_request_in_window_is_not_cold_if_the_transcript_started_earlier(self):
        self.main_file(pair(gap=600))
        rep, rebuilds = self.compute(since=BASE + 300)
        self.assertEqual(rep["rebuilds"]["cold_starts"]["count"], 0)
        self.assertEqual(len(rebuilds), 1)


class TestRebuilds(Env):
    def one(self, lines, **kw):
        self.main_file(lines)
        rep, rebuilds = self.compute(**kw)
        return rep, rebuilds

    def test_cold_start_is_not_a_rebuild(self):
        rep, rebuilds = self.one([asst(0, "m1", cw=100000, cw5m=100000)])
        self.assertEqual(rebuilds, [])
        self.assertEqual(rep["rebuilds"]["cold_starts"]["count"], 1)
        self.assertGreater(rep["rebuilds"]["cold_starts"]["write_cost"], 0)

    def test_normal_cache_growth_is_not_a_rebuild(self):
        _, rebuilds = self.one([asst(0, "m1", cw=100000, cw5m=100000),
                                asst(30, "m2", cr=100000, cw=2000, cw5m=2000)])
        self.assertEqual(rebuilds, [])

    def test_cause_expired_and_gap_buckets(self):
        for gap, bucket in ((600, "5-60m"), (4000, "1-8h"), (40000, ">8h")):
            _, rebuilds = self.one(pair(gap=gap))
            self.assertEqual([(b.cause, b.bucket) for b in rebuilds], [("expired", bucket)], gap)

    def test_cause_prefix_change_under_ttl(self):
        _, rebuilds = self.one(pair(gap=60))
        self.assertEqual([(b.cause, b.bucket) for b in rebuilds], [("prefix_change", "<5m")])

    def test_one_hour_writes_extend_the_ttl(self):
        _, rebuilds = self.one(pair(gap=600, prev_1h=True))
        self.assertEqual(rebuilds[0].cause, "prefix_change")
        _, rebuilds = self.one(pair(gap=4000, prev_1h=True))
        self.assertEqual(rebuilds[0].cause, "expired")

    def test_ttl_comes_from_the_last_request_that_wrote(self):
        # a 1h write, then a pure read, then a rebuild 600s later: still under the 1h TTL
        lines = [asst(0, "m1", cw=100000, cw1h=100000, cw5m=0),
                 asst(60, "m2", cr=100000, inp=1),
                 asst(700, "m3", cw=100000, cw5m=100000, inp=1)]
        _, rb = self.one(lines)
        self.assertEqual(len(rb), 1)
        self.assertEqual(rb[0].cause, "prefix_change")
        # the same chain with a 5m write expires
        lines[0] = asst(0, "m1", cw=100000, cw5m=100000)
        _, rb = self.one(lines)
        self.assertEqual(rb[0].cause, "expired")

    def test_cause_model_switch(self):
        _, rebuilds = self.one(pair(gap=60, model2="claude-sonnet-5"))
        self.assertEqual(rebuilds[0].cause, "model_switch")
        # a model switch wins over an expired cache
        _, rebuilds = self.one(pair(gap=600, model2="claude-sonnet-5"))
        self.assertEqual(rebuilds[0].cause, "model_switch")

    def test_cause_compaction_wins_and_records_the_kind(self):
        for lines_extra, kind in (([boundary(30, "auto")], "auto"), ([boundary(30, "manual")], "manual"),
                                  ([boundary(30, "auto"), idle_marker(31)], "idle")):
            a, b = pair(gap=600, model2="claude-sonnet-5")
            _, rebuilds = self.one([a] + lines_extra + [b])
            self.assertEqual((rebuilds[0].cause, rebuilds[0].compaction_kind), ("compaction", kind))

    def test_headline_leaves_out_compaction(self):
        a, b = pair(gap=600)
        c = asst(1200, "m3", cw=100000, cw5m=100000, inp=1)  # an expired rebuild after the compaction one
        rep, rebuilds = self.one([a, boundary(30, "auto"), b, c])
        self.assertEqual({x.cause for x in rebuilds}, {"compaction", "expired"})
        h = rep["rebuilds"]["headline"]
        comp, expired = h["by_cause"]["compaction"], h["by_cause"]["expired"]
        self.assertEqual((comp["count"], expired["count"]), (1, 1))
        self.assertEqual((h["count"], h["rewritten_tokens"]), (1, expired["rewritten_tokens"]))
        self.assertAlmostEqual(h["premium"], expired["premium"], places=4)
        self.assertAlmostEqual(h["pct_of_total_cost"], expired["pct_of_total_cost"], places=1)
        self.assertGreater(comp["premium"], 0)
        self.assertAlmostEqual(sum(x["premium"] for x in h["by_who"].values()), h["premium"], places=4)
        self.assertAlmostEqual(sum(x["premium"] for x in h["slices"]), h["premium"], places=4)

    def test_waiting_on(self):
        _, rb = self.one(pair(gap=600, prev_stop="end_turn"))
        self.assertEqual(rb[0].waiting, "user")
        _, rb = self.one(pair(gap=600, prev_stop="tool_use", prev_tools=("Bash",)))
        self.assertEqual(rb[0].waiting, "tool:Bash")

    def test_waiting_on_other_stop_reasons(self):
        _, rb = self.one(pair(gap=600, prev_stop="max_tokens"))
        self.assertEqual(rb[0].waiting, "stop:max_tokens")
        _, rb = self.one(pair(gap=600, prev_stop=None))
        self.assertEqual(rb[0].waiting, "stop:none")
        rep, _ = self.one(pair(gap=600, prev_stop="max_tokens"))
        self.assertEqual(rep["rebuilds"]["headline"]["subagent_tool_waits"], [])

    def test_rewritten_and_premium_numbers(self):
        # prev prompt 100K, this request reads nothing and writes 120K: 100K were rewritten
        a = asst(0, "m1", cw=100000, cw5m=100000)
        b = asst(600, "m2", cw=120000, cw5m=120000)
        _, rb = self.one([a, b])
        self.assertEqual(rb[0].rewritten, 100000)
        # opus-5: write 6.25, read 0.50 per MTok
        self.assertAlmostEqual(rb[0].premium, 100000 * (6.25 - 0.5) / 1e6, places=6)

    def test_rewrite_is_capped_by_what_was_written(self):
        a = asst(0, "m1", cw=100000, cw5m=100000)
        b = asst(600, "m2", cr=0, cw=30000, cw5m=30000, inp=70000)
        _, rb = self.one([a, b])
        self.assertEqual(rb[0].rewritten, 30000)

    def test_post_rule_cross_check(self):
        rep, _ = self.one(pair(gap=600))
        pr = rep["rebuilds"]["post_rule"]
        self.assertEqual(pr["count"], 1)
        self.assertEqual(pr["by_who_gap"][0]["gap"], "5-60m")
        self.assertAlmostEqual(pr["premium"], 100000 * (6.25 - 0.5) / 1e6, places=4)

    def test_slices_key_set(self):
        rep, _ = self.one(pair(gap=600, prev_stop="tool_use", prev_tools=("Bash",)))
        s = rep["rebuilds"]["headline"]["slices"][0]
        self.assertEqual({k for k in s}, {"cause", "who", "gap", "waiting_on", "count", "rewritten_tokens", "premium"})
        self.assertEqual((s["cause"], s["who"], s["gap"], s["waiting_on"]), ("expired", "main", "5-60m", "tool:Bash"))


class TestHitRate(Env):
    def test_hit_and_rebuild_by_gap(self):
        self.main_file([
            asst(0, "m1", cw=100000, cw5m=100000),
            asst(60, "m2", cr=100000, cw=1000, cw5m=1000),      # hit after 1 minute
            asst(1000, "m3", cw=101000, cw5m=101000),            # rebuild after ~16 minutes
        ])
        rep, _ = self.compute()
        rows = {(r["who"], r["gap"]): r for r in rep["hit_rate"]["rows"]}
        self.assertEqual(rows[("main", "0-5m")]["hit_pct"], 100.0)
        self.assertEqual(rows[("main", "5-30m")]["rebuild_pct"], 100.0)


class TestContext(Env):
    def test_bucket_edges_are_lower_inclusive(self):
        self.main_file([asst(0, "m1", cr=199999), asst(1, "m2", cr=200000), asst(2, "m3", cr=399999),
                        asst(3, "m4", cr=400000)])
        rep, _ = self.compute()
        self.assertEqual([b["requests"] for b in rep["context_size"]], [1, 2, 1])

    def test_buckets(self):
        self.main_file([asst(0, "m1", cr=100000), asst(1, "m2", cr=300000), asst(2, "m3", cr=500000)])
        rep, _ = self.compute()
        self.assertEqual([b["requests"] for b in rep["context_size"]], [1, 1, 1])
        self.assertAlmostEqual(sum(b["pct"] for b in rep["context_size"]), 100, places=1)


class TestAgentPeakContext(Env):
    def rows(self, rep):
        return {r["bucket"]: r for r in rep["agent_peak_context"]["rows"]}

    def test_bucket_edges_are_lower_inclusive(self):
        for i, pt in enumerate((99999, 100000, 249999, 250000, 499999, 500000)):
            self.sub_file([asst(i, "s%d" % i, cr=pt)], agent="agent-%d" % i)
        rep, _ = self.compute()
        ap = rep["agent_peak_context"]
        self.assertEqual([r["bucket"] for r in ap["rows"]], ["<100K", "100-250K", "250-500K", ">=500K"])
        self.assertEqual([r["agents"] for r in ap["rows"]], [1, 2, 2, 1])
        self.assertEqual(ap["agents"], 6)
        self.assertEqual(ap["agents"], rep["totals"]["transcripts_subagent"])

    def test_peak_is_input_plus_cache_read_plus_cache_write(self):
        self.sub_file([asst(0, "s1", inp=30000, cr=40000, cw=30000, cw5m=30000)])
        rep, _ = self.compute()
        self.assertEqual(self.rows(rep)["100-250K"]["agents"], 1)

    def test_main_session_is_excluded(self):
        self.main_file([asst(0, "m1", cr=600000)])
        self.sub_file([asst(1, "s1", cr=1000)])
        rep, _ = self.compute()
        ap = rep["agent_peak_context"]
        self.assertEqual(ap["agents"], 1)
        self.assertEqual(self.rows(rep)[">=500K"]["agents"], 0)
        self.assertAlmostEqual(ap["cost"], rep["by_kind"]["subagent"]["cost"], places=4)

    def test_only_in_window_requests_count_for_peak_and_cost(self):
        self.sub_file([asst(0, "s1", cr=600000), asst(100, "s2", cr=50000), asst(200, "s3", cr=60000)])
        rep, _ = self.compute(since=BASE + 50)
        ap = rep["agent_peak_context"]
        self.assertEqual([r["agents"] for r in ap["rows"]], [1, 0, 0, 0])
        inwin = sum(r["cost"] for r in [rep["by_kind"]["subagent"]])
        self.assertAlmostEqual(ap["cost"], inwin, places=4)
        full, _ = self.compute()
        self.assertEqual(self.rows(full)[">=500K"]["agents"], 1)
        self.assertGreater(full["agent_peak_context"]["cost"], ap["cost"])

    def test_agent_wholly_outside_the_window_is_not_counted(self):
        self.sub_file([asst(0, "s1", cr=600000)])
        rep, _ = self.compute(since=BASE + 50)
        self.assertEqual(rep["agent_peak_context"]["agents"], 0)

    def test_empty_buckets_are_still_present(self):
        self.sub_file([asst(0, "s1", cr=1000)])
        rep, _ = self.compute()
        rows = rep["agent_peak_context"]["rows"]
        self.assertEqual(len(rows), 4)
        self.assertEqual([r["agents"] for r in rows], [1, 0, 0, 0])
        self.assertEqual(rows[1]["cost"], 0)
        self.assertEqual(rows[1]["pct"], 0.0)

    def test_no_agents_at_all(self):
        self.main_file([asst(0, "m1", cr=1000)])
        rep, _ = self.compute()
        ap = rep["agent_peak_context"]
        self.assertEqual((ap["agents"], ap["cost"]), (0, 0))
        self.assertEqual([r["agents"] for r in ap["rows"]], [0, 0, 0, 0])

    def test_shares_are_of_agents_and_of_agent_cost(self):
        self.sub_file([asst(0, "s1", cr=1000)], agent="agent-a")
        self.sub_file([asst(1, "s2", cr=600000)], agent="agent-b")
        self.main_file([asst(2, "m1", cr=600000)])
        rep, _ = self.compute()
        rows = self.rows(rep)
        self.assertEqual(rows["<100K"]["agents_pct"], 50.0)
        self.assertAlmostEqual(rows["<100K"]["pct"] + rows[">=500K"]["pct"], 100, places=1)
        self.assertGreater(rows[">=500K"]["pct"], 90)

    def test_try_this_when_big_agents_dominate_cost(self):
        self.sub_file([asst(0, "s1", cr=600000)], agent="agent-b")
        rep, _ = self.compute()
        self.assertTrue(any("passed 500K" in x for x in rep["try_this"]), rep["try_this"])

    def test_no_try_this_when_small_agents_dominate_cost(self):
        self.sub_file([asst(0, "s1", cr=1000)], agent="agent-a")
        rep, _ = self.compute()
        self.assertFalse(any("passed 500K" in x for x in rep["try_this"]))


class TestIdleCompaction(Env):
    def test_saving_estimate(self):
        self.main_file([
            asst(0, "m1", cr=190000, cw=10000, cw5m=10000),
            boundary(10000, "auto", pre=200000, post=10000), idle_marker(10001),
            asst(20000, "m2", cw=30000, cw5m=30000),  # the observed write differs from postTokens
        ])
        rep, _ = self.compute()
        ic = rep["idle_compactions"]
        self.assertEqual((ic["count"], ic["followed_by_request"], ic["never_followed"]), (1, 1, 0))
        self.assertEqual(ic["cache_still_warm"], 0)
        # opus-5: read 0.5, output 25, write 6.25 per MTok
        cost = 200000 * 0.5e-6 + 10000 * 25e-6
        self.assertAlmostEqual(ic["compaction_cost_estimate"], cost, places=2)
        expected = 200000 * 6.25e-6 - (cost + 30000 * 6.25e-6)
        self.assertAlmostEqual(ic["estimated_saving"], expected, places=2)
        self.assertTrue(ic["estimated"])

    def test_no_saving_when_the_cache_was_still_warm(self):
        for ttl_kw, nxt, expired in (({"cw5m": 10000}, 3000, True),   # 5m cache, back after 50 min
                                     ({"cw5m": 10000}, 200, False),   # 5m cache, back after 200 s
                                     ({"cw1h": 10000}, 3000, False),  # 1h cache, back after 50 min
                                     ({"cw1h": 10000}, 3700, True)):  # 1h cache, back after 62 min
            self.main_file([
                asst(0, "m1", cr=190000, cw=10000, **ttl_kw),
                boundary(nxt - 100, "auto", pre=200000, post=10000), idle_marker(nxt - 99),
                asst(nxt, "m2", cw=30000, cw5m=30000),
            ])
            rep, _ = self.compute()
            ic = rep["idle_compactions"]
            if expired:
                self.assertEqual(ic["cache_still_warm"], 0)
                self.assertGreater(ic["estimated_saving"], 0)
            else:
                self.assertEqual(ic["cache_still_warm"], 1)
                self.assertEqual(ic["estimated_saving"], 0)
                self.assertGreater(ic["cache_still_warm_cost_estimate"], 0)

    def test_never_followed_is_pure_cost(self):
        self.main_file([
            asst(0, "m1", cr=190000, cw=10000, cw5m=10000),
            boundary(10000, "auto", pre=200000, post=10000), idle_marker(10001),
        ])
        rep, _ = self.compute()
        ic = rep["idle_compactions"]
        self.assertEqual((ic["followed_by_request"], ic["never_followed"]), (0, 1))
        self.assertGreater(ic["never_followed_cost_estimate"], 0)
        self.assertEqual(ic["estimated_saving"], 0)

    def test_non_idle_compactions_are_not_in_the_idle_section(self):
        self.main_file([asst(0, "m1", cw=10, cw5m=10), boundary(5, "auto"), boundary(6, "manual")])
        rep, _ = self.compute()
        self.assertEqual(rep["idle_compactions"]["count"], 0)
        self.assertEqual(rep["compactions"], {"auto": 1, "manual": 1, "idle": 0})


class TestOneHour(Env):
    def sub_run(self, gap, n=120, rebuild=True):
        lines = []
        t = 0
        for i in range(n):
            if rebuild or i == 0:
                lines.append(asst(t, "s%d" % i, cw=50000, cw5m=50000, inp=1, model="claude-opus-5-5"))
            else:
                lines.append(asst(t, "s%d" % i, cr=50001 + 1000 * (i - 1), cw=1000, cw5m=1000, model="claude-opus-5-5"))
            t += gap
        self.sub_file(lines, meta={"agentType": "implementer"})
        rep, _ = self.compute()
        return rep["one_hour_subagent_cache"]["subagent"]

    def test_break_even_exactly(self):
        self.assertEqual(analysis.breakeven(0.05), 0.75 / 1.95)
        self.assertAlmostEqual(analysis.breakeven(0.05), 0.3846, places=3)

    def test_pays_off_when_rebuilds_dominate(self):
        v = self.sub_run(600)
        self.assertEqual(v["verdict"], "would pay off")
        self.assertGreater(v["r"], v["break_even"])
        self.assertEqual(v["break_even"], round(0.75 / 1.95, 4))
        self.assertGreater(v["net"], 0)

    def test_does_not_pay_off_without_rebuilds(self):
        v = self.sub_run(60, rebuild=False)
        self.assertEqual(v["verdict"], "would not pay off")
        self.assertLess(v["net"], 0)

    def test_too_little_data(self):
        v = self.sub_run(600, n=20)
        self.assertTrue(v["verdict"].startswith("too little data"))

    def test_counterfactual_matches_hand_calculation(self):
        # two requests, 10 minutes apart, 50K written each time at 5m: extra = 100K * 0.75 * $5
        self.sub_file([asst(0, "s0", cw=50000, cw5m=50000, inp=1), asst(600, "s1", cw=50001, cw5m=50001)],
                      meta={"agentType": "implementer"})
        rep, _ = self.compute()
        a = rep["one_hour_subagent_cache"]["all_subagents"]
        self.assertAlmostEqual(a["extra_write_premium"], 100001 * 0.75 * 5e-6, places=4)
        # rebuilt = cw - max(0, pt - prev_pt) = 50001 - 1 ... saved at (2*5 - 0.5) per MTok
        pt_prev, pt = 50001, 50001
        rebuilt = 50001 - max(0, pt - pt_prev)
        self.assertAlmostEqual(a["rebuilds_avoided"], rebuilt * (10 - 0.5) * 1e-6, places=4)

    def test_try_this_lines_end_with_the_post_url(self):
        self.sub_run(600)
        rep, _ = self.compute()
        self.assertTrue(rep["try_this"])
        for line in rep["try_this"]:
            self.assertTrue(line.endswith(analysis.POST_URL))


if __name__ == "__main__":
    unittest.main()
