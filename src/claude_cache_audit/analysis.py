"""Cost profile, cache rebuilds, idle compactions and the 1-hour subagent verdict.

Everything here works on the request records from transcripts.py and returns plain
dicts (the JSON report). Nothing in this module reads files or prints.
"""

from __future__ import annotations

import bisect
import collections
import datetime as dt

from . import __version__, pricing

SCHEMA_VERSION = 1
POST_URL = "https://27medkamal.com/claude-code-usage/"

# A request counts as a rebuild under the post's rule when it wrote at least this much...
POST_RULE_MIN_CW = 20_000
# ...and at least this share of its prompt.
POST_RULE_MIN_SHARE = 0.5
# Headline measure: ignore rewrites smaller than this (cache-boundary rounding noise).
MIN_REWRITTEN = 1_000

TTL_5M = 300
TTL_1H = 3600
GAP_BUCKETS = ("<5m", "5-60m", "1-8h", ">8h")
HIT_BUCKETS = ((0, 5), (5, 30), (30, 55), (55, 65), (65, 120), (120, 1e9))
CAUSES = ("compaction", "model_switch", "expired", "prefix_change")
KINDS = ("main", "subagent", "teammate", "fork")
SUB_KINDS = ("subagent", "teammate", "fork")
CONTEXT_BUCKETS = (("<200K", 0, 200_000), ("200-400K", 200_000, 400_000), (">=400K", 400_000, float("inf")))
PEAK_BUCKETS = (("<100K", 0, 100_000), ("100-250K", 100_000, 250_000), ("250-500K", 250_000, 500_000),
                (">=500K", 500_000, float("inf")))
MIN_VERDICT_REQUESTS = 100
TRY_TOOL_WAIT_SHARE = 0.05
TRY_BIG_CONTEXT_SHARE = 0.25
TRY_BIG_AGENT_SHARE = 0.40


def breakeven(read_ratio):
    """Share of cache-write tokens that must be avoidable rebuilds for 1h writes to pay off.

    1h writes cost 2x input instead of 1.25x (+0.75x on every write). Each avoided rebuild
    turns a 2x write into a read at `read_ratio` x input. Break-even: r * (2 - read_ratio) = 0.75.
    """
    return 0.75 / (2 - read_ratio)


# ---------------------------------------------------------------------------- preparation

def gap_bucket(gap):
    if gap < TTL_5M:
        return "<5m"
    if gap < TTL_1H:
        return "5-60m"
    if gap < 8 * 3600:
        return "1-8h"
    return ">8h"


def classify_kind(r, meta):
    if not r.is_sub:
        return "main", "main"
    m = meta.get(r.key) or {}
    atype = m.get("agentType")
    if atype == "fork":
        return "fork", atype
    if m.get("taskKind") == "in_process_teammate":
        return "teammate", atype or "teammate"
    return "subagent", atype or "subagent"


def prepare(scan):
    """Price every request, classify it and link each one to its predecessor.

    Returns (requests sorted by time within each transcript, {key: [requests]}, set of unknown
    models, tokens of cache writes with no TTL split).
    """
    unknown = collections.Counter()
    unknown_ttl = 0
    for r in scan.requests:
        cw1h, cw5m = r.cw1h, r.cw5m
        if cw1h is None and cw5m is None:
            cw1h, cw5m = 0, r.cw
            if r.cw:
                unknown_ttl += r.cw
        r.cw1h, r.cw5m = cw1h or 0, cw5m or 0
        r.pt = r.inp + r.cr + r.cw
        p = pricing.price(r.model, r.pt, r.speed)
        if p is None:
            p = (0, 0, 0)
            r.unknown_model = True
            unknown[r.model] += 1
        r.price_in, r.price_out, r.price_cr = (x / 1e6 for x in p)
        r.c_in = r.inp * r.price_in
        r.c_cw5m = r.cw5m * pricing.WRITE_5M * r.price_in
        r.c_cw1h = r.cw1h * pricing.WRITE_1H * r.price_in
        r.c_cw = r.c_cw5m + r.c_cw1h
        r.c_cr = r.cr * r.price_cr
        r.c_out = r.out * r.price_out
        r.cost = r.c_in + r.c_cw + r.c_cr + r.c_out
        r.fam = pricing.family(r.model)
        r.kind, r.agent_type = classify_kind(r, scan.meta)
    by = collections.defaultdict(list)
    for r in scan.requests:
        by[r.key].append(r)
    for rs in by.values():
        rs.sort(key=lambda r: r.ts or 0)
        prev = None
        for r in rs:
            r.prev = prev
            prev = r
    return by, unknown, unknown_ttl


# ---------------------------------------------------------------------------- helpers

def _pct(x, y):
    return round(100.0 * x / y, 2) if y else 0.0


def _r(x, n=4):
    return round(x, n)


def write_price_per_token(r):
    """Blended cache-write price of this request, $ per token."""
    if r.cw:
        return r.c_cw / r.cw
    return r.price_in * pricing.WRITE_5M


class Rebuild:
    __slots__ = ("req", "prev", "cause", "compaction_kind", "gap", "bucket", "waiting", "rewritten",
                 "premium", "write_cost")


def waiting_on(prev):
    if prev.stop == "end_turn":
        return "user"
    if prev.tools:
        return "tool:" + prev.tools[0]
    return "stop:" + (prev.stop or "none")


def previous_ttl(prev):
    """TTL of the cache entry the next request would hit: from the latest request that wrote anything."""
    p = prev
    while p is not None:
        if p.cw > 0:
            return TTL_1H if p.cw1h > 0 else TTL_5M
        p = p.prev
    return TTL_5M


def find_rebuilds(by, compactions):
    """Headline rebuilds for every non-cold request, keyed by request identity."""
    cmap = collections.defaultdict(list)
    for c in compactions:
        if c.ts is not None:
            cmap[c.key].append(c)
    cts = {}
    for k, cs in cmap.items():
        cs.sort(key=lambda c: c.ts)
        cts[k] = [c.ts for c in cs]
    out = {}
    for key, rs in by.items():
        for r in rs:
            p = r.prev
            if p is None:
                continue
            rewritten = max(0, min(p.pt - r.cr, r.cw))
            if rewritten < MIN_REWRITTEN:
                continue
            gap = (r.ts or 0) - (p.ts or 0)
            ckind = None
            cause = None
            if key in cts:
                lo = bisect.bisect_right(cts[key], p.ts or 0)
                hi = bisect.bisect_right(cts[key], r.ts or 0)
                if hi > lo:
                    cause, ckind = "compaction", cmap[key][hi - 1].kind
            if cause is None:
                if p.model != r.model:
                    cause = "model_switch"
                elif gap >= previous_ttl(p):
                    cause = "expired"
                else:
                    cause = "prefix_change"
            rb = Rebuild()
            rb.req, rb.prev, rb.cause, rb.compaction_kind = r, p, cause, ckind
            rb.gap, rb.bucket, rb.waiting = gap, gap_bucket(gap), waiting_on(p)
            rb.rewritten = rewritten
            wp = write_price_per_token(r)
            rb.write_cost = rewritten * wp
            rb.premium = rewritten * (wp - r.price_cr)
            out[id(r)] = rb
    return out


def post_rule(r):
    return r.cw >= POST_RULE_MIN_CW and r.cw >= POST_RULE_MIN_SHARE * r.pt


# ---------------------------------------------------------------------------- window

class Window:
    def __init__(self, since=None, until=None):
        self.since, self.until = since, until

    def has(self, ts):
        if self.since is None and self.until is None:
            return True
        if ts is None:
            return False
        if self.since is not None and ts < self.since:
            return False
        if self.until is not None and ts >= self.until:
            return False
        return True


# ---------------------------------------------------------------------------- the report

def compute(scan, since=None, until=None, now=None):
    """Return (report dict, [Rebuild] costliest first)."""
    by, unknown, unknown_ttl = prepare(scan)
    win = Window(since, until)
    allreqs = [r for rs in by.values() for r in rs]
    reqs = [r for r in allreqs if win.has(r.ts)]
    comps = [c for c in scan.compactions if win.has(c.ts)]
    rebuild_map = find_rebuilds(by, scan.compactions)
    rebuilds = [rebuild_map[id(r)] for r in reqs if id(r) in rebuild_map]
    total = sum(r.cost for r in reqs)

    rep = collections.OrderedDict()
    rep["schema_version"] = SCHEMA_VERSION
    rep["tool_version"] = __version__
    rep["pricing"] = {"as_of": pricing.AS_OF, "source": pricing.SOURCE_URL, "basis": "API-equivalent list prices"}
    rep["window"] = _window(reqs, since, until, now)
    rep["totals"] = _totals(reqs, scan, unknown, unknown_ttl, total)
    rep["usage_split"] = _usage_split(reqs, total)
    rep["by_kind"] = _by_kind(reqs, total)
    rep["agent_peak_context"] = _agent_peak_context(reqs)
    rep["model_mix"] = _model_mix(reqs, total)
    rep["rebuilds"] = _rebuilds(reqs, rebuilds, total, rebuild_map)
    rep["hit_rate"] = _hit_rate(reqs)
    rep["context_size"] = _context(reqs, total)
    rep["compactions"] = _compactions(comps, by)
    rep["idle_compactions"] = _idle(comps, by)
    rep["one_hour_subagent_cache"] = _one_hour(reqs, rebuilds)
    rep["try_this"] = _try_this(rep, total, reqs, rebuilds)
    rebuilds.sort(key=lambda b: -b.premium)
    return rep, rebuilds


def _iso(ts):
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes") if ts else None


def _window(reqs, since, until, now):
    tss = [r.ts for r in reqs if r.ts]
    first = min(tss) if tss else None
    last = max(tss) if tss else None
    start = since if since is not None else first
    end = until if until is not None else (now if now is not None else last)
    days = round((end - start) / 86400.0, 1) if start and end else 0
    return {"since": _iso(since), "until": _iso(until), "first_request": _iso(first),
            "last_request": _iso(last), "days": days}


def _version_key(v):
    out = []
    for p in str(v).split("."):
        out.append(int(p) if p.isdigit() else 0)
    return tuple(out)


def _totals(reqs, scan, unknown, unknown_ttl, total):
    vers = sorted({r.version for r in reqs if r.version}, key=_version_key)
    keys_main = {r.key for r in reqs if not r.is_sub}
    keys_sub = {r.key for r in reqs if r.is_sub}
    return {
        "requests": len(reqs),
        "cost": _r(total, 4),
        "transcripts_main": len(keys_main),
        "transcripts_subagent": len(keys_sub),
        "files_scanned": scan.files,
        "files_skipped_old": scan.skipped_old,
        "claude_code_versions": {"min": vers[0] if vers else None, "max": vers[-1] if vers else None},
        "malformed_lines": scan.malformed,
        "unknown_models": dict(sorted(unknown.items(), key=lambda kv: -kv[1])),
        "write_tokens_without_ttl_split": unknown_ttl,
    }


def _usage_split(reqs, total):
    def part(cost, tokens):
        return {"cost": _r(cost, 4), "pct": _pct(cost, total), "tokens": tokens}

    out_tok = sum(r.out for r in reqs)
    think = sum(r.think for r in reqs)
    split = collections.OrderedDict()
    split["cache_read"] = part(sum(r.c_cr for r in reqs), sum(r.cr for r in reqs))
    split["cache_write_5m"] = part(sum(r.c_cw5m for r in reqs), sum(r.cw5m for r in reqs))
    split["cache_write_1h"] = part(sum(r.c_cw1h for r in reqs), sum(r.cw1h for r in reqs))
    split["uncached_input"] = part(sum(r.c_in for r in reqs), sum(r.inp for r in reqs))
    split["output"] = part(sum(r.c_out for r in reqs), out_tok)
    split["output"]["thinking_tokens"] = think
    split["output"]["thinking_pct_of_output_tokens"] = _pct(think, out_tok)
    return split


def _by_kind(reqs, total):
    out = collections.OrderedDict()
    for k in KINDS:
        rs = [r for r in reqs if r.kind == k]
        cost = sum(r.cost for r in rs)
        out[k] = {"cost": _r(cost, 4), "pct": _pct(cost, total), "requests": len(rs),
                  "transcripts": len({r.key for r in rs})}
    return out


def _model_mix(reqs, total):
    c = collections.Counter()
    for r in reqs:
        c[r.fam] += r.cost
    return {k: _pct(v, total) for k, v in sorted(c.items(), key=lambda kv: -kv[1])}


def _slice_key(rb):
    return (rb.cause, rb.req.kind, rb.bucket, rb.waiting)


def _rebuilds(reqs, rebuilds, total, rebuild_map):
    cold = [r for r in reqs if r.prev is None]
    cold_cost = sum(r.c_cw for r in cold)

    # the post's rule, as a cross-check: every non-cold request writing >= 20K and >= half its prompt
    pr_n, pr_prem = 0, 0.0
    pr_by = collections.defaultdict(lambda: [0, 0.0])
    for r in reqs:
        p = r.prev
        if p is None or not post_rule(r):
            continue
        prem = r.c_cw - r.cw * r.price_cr
        who = "subagent" if r.is_sub else "main"
        bucket = gap_bucket((r.ts or 0) - (p.ts or 0))
        pr_n += 1
        pr_prem += prem
        pr_by[(who, bucket)][0] += 1
        pr_by[(who, bucket)][1] += prem

    # headline measure
    # compaction rebuilds are new content, not an avoidable rebuild: reported apart, never in the headline
    hl = [b for b in rebuilds if b.cause != "compaction"]
    tot_rw = sum(b.rewritten for b in hl)
    tot_prem = sum(b.premium for b in hl)
    by_cause = collections.OrderedDict()
    for c in CAUSES:
        bs = [b for b in rebuilds if b.cause == c]
        d = {"count": len(bs), "rewritten_tokens": sum(b.rewritten for b in bs),
             "premium": _r(sum(b.premium for b in bs), 4),
             "pct_of_total_cost": _pct(sum(b.premium for b in bs), total)}
        if c == "compaction":
            d["by_compaction_kind"] = {k: sum(1 for b in bs if b.compaction_kind == k) for k in ("auto", "manual", "idle")}
        by_cause[c] = d
    slices = collections.defaultdict(lambda: [0, 0, 0.0])
    for b in hl:
        s = slices[_slice_key(b)]
        s[0] += 1
        s[1] += b.rewritten
        s[2] += b.premium
    slice_list = [
        {"cause": k[0], "who": k[1], "gap": k[2], "waiting_on": k[3], "count": v[0],
         "rewritten_tokens": v[1], "premium": _r(v[2], 4)}
        for k, v in sorted(slices.items(), key=lambda kv: -kv[1][2])
    ]

    def group(fn):
        g = collections.defaultdict(lambda: [0, 0.0])
        for b in hl:
            x = g[fn(b)]
            x[0] += 1
            x[1] += b.premium
        return {k: {"count": v[0], "premium": _r(v[1], 4)} for k, v in sorted(g.items(), key=lambda kv: -kv[1][1])}

    # who waited on which tool, subagents, expired or not
    tw = collections.defaultdict(lambda: [0, 0.0, 0.0])
    for b in hl:
        if b.req.is_sub and b.waiting.startswith("tool:"):
            x = tw[b.waiting[5:]]
            x[0] += 1
            x[1] += b.premium
            x[2] += b.write_cost
    tools = [{"tool": k, "count": v[0], "premium": _r(v[1], 4), "write_cost": _r(v[2], 4)}
             for k, v in sorted(tw.items(), key=lambda kv: -kv[1][1])[:10]]

    return {
        "cold_starts": {"count": len(cold), "write_cost": _r(cold_cost, 4),
                        "pct_of_total_cost": _pct(cold_cost, total)},
        "post_rule": {
            "rule": "cache write >= 20K tokens and >= 50% of the prompt, not the first request of a transcript",
            "count": pr_n, "premium": _r(pr_prem, 4), "pct_of_total_cost": _pct(pr_prem, total),
            "by_who_gap": [{"who": k[0], "gap": k[1], "count": v[0], "premium": _r(v[1], 4)}
                           for k, v in sorted(pr_by.items(), key=lambda kv: -kv[1][1])],
        },
        "headline": {
            "measure": "tokens that were already cached and got rewritten, priced at write minus read; "
                       "compaction rebuilds are left out (see by_cause.compaction)",
            "min_rewritten_tokens": MIN_REWRITTEN,
            "count": len(hl), "rewritten_tokens": tot_rw, "premium": _r(tot_prem, 4),
            "pct_of_total_cost": _pct(tot_prem, total),
            "by_cause": by_cause,
            "by_who": group(lambda b: b.req.kind),
            "by_gap": group(lambda b: b.bucket),
            "by_waiting_on": dict(list(group(lambda b: b.waiting).items())[:8]),
            "slices": slice_list,
            "subagent_tool_waits": tools,
        },
    }


def _hit_rate(reqs):
    stat = collections.defaultdict(lambda: [0, 0, 0])
    ttl = {"main": [0, 0], "subagent": [0, 0]}

    def label(g):
        for a, b in HIT_BUCKETS:
            if a <= g < b:
                return "%d-%dm" % (a, b) if b < 1e9 else ">%dm" % a

    for r in reqs:
        who = "subagent" if r.is_sub else "main"
        ttl[who][0] += r.cw1h
        ttl[who][1] += r.cw5m
        p = r.prev
        if p is None or p.model != r.model or r.pt < 30_000:
            continue
        lab = label(((r.ts or 0) - (p.ts or 0)) / 60.0)
        s = stat[(who, lab)]
        if r.cr >= 0.5 * r.pt:
            s[0] += 1
        elif r.cw >= 0.5 * r.pt:
            s[1] += 1
        else:
            s[2] += 1
    rows = []
    for who in ("main", "subagent"):
        for a, b in HIT_BUCKETS:
            lab = "%d-%dm" % (a, b) if b < 1e9 else ">%dm" % a
            h, rb, ot = stat.get((who, lab), [0, 0, 0])
            n = h + rb + ot
            if n:
                rows.append({"who": who, "gap": lab, "requests": n, "hit_pct": _pct(h, n), "rebuild_pct": _pct(rb, n)})
    return {
        "definition": "requests with a prompt of at least 30K tokens on the same model as the previous request; "
                      "hit = at least half the prompt read from cache, rebuild = at least half written",
        "cache_write_tokens_by_ttl": {k: {"1h": v[0], "5m": v[1]} for k, v in ttl.items()},
        "rows": rows,
    }


def _context(reqs, total):
    out = []
    for name, lo, hi in CONTEXT_BUCKETS:
        rs = [r for r in reqs if lo <= r.pt < hi]
        c = sum(r.cost for r in rs)
        out.append({"bucket": name, "requests": len(rs), "cost": _r(c, 4), "pct": _pct(c, total)})
    return out


def _agent_peak_context(reqs):
    """Agents (non-main transcripts) bucketed by their largest prompt in the window, with their cost."""
    peak, cost = {}, collections.defaultdict(float)
    for r in reqs:
        if r.is_sub:
            peak[r.key] = max(peak.get(r.key, 0), r.pt)
            cost[r.key] += r.cost
    total = sum(cost.values())
    rows = []
    for name, lo, hi in PEAK_BUCKETS:
        keys = [k for k, p in peak.items() if lo <= p < hi]
        c = sum(cost[k] for k in keys)
        rows.append({"bucket": name, "agents": len(keys), "agents_pct": _pct(len(keys), len(peak)),
                     "cost": _r(c, 4), "pct": _pct(c, total)})
    return {"definition": "one agent = one non-main transcript; peak = its largest prompt (input + cache read + "
                          "cache write) among requests in the window; cost = those requests' API-equivalent cost",
            "agents": len(peak), "cost": _r(total, 4), "rows": rows}


def _compactions(comps, by):
    c = collections.Counter(x.kind for x in comps)
    return {k: c.get(k, 0) for k in ("auto", "manual", "idle")}


def _neighbours(by, c):
    """Last request at or before the compaction and first request after it, same transcript."""
    rs = by.get(c.key) or []
    ts = [r.ts or 0 for r in rs]
    i = bisect.bisect_right(ts, c.ts or 0)
    before = rs[i - 1] if i > 0 else None
    after = rs[i] if i < len(rs) else None
    return before, after


def compaction_cost_estimate(c, before):
    """The compaction call is not logged, so price it: read `pre` tokens, write a summary of about `post` tokens."""
    if before is None or c.pre is None:
        return 0.0
    return c.pre * before.price_cr + (c.post or 0) * before.price_out


def _idle(comps, by):
    idle = [c for c in comps if c.kind == "idle"]
    comp_cost = 0.0
    saving = 0.0
    followed = never = unknown = warm = 0
    never_cost = warm_cost = 0.0
    for c in idle:
        before, after = _neighbours(by, c)
        if c.pre is None or before is None:
            unknown += 1
            continue
        cost = compaction_cost_estimate(c, before)
        comp_cost += cost
        if after is None:
            never += 1
            never_cost += cost
            continue
        followed += 1
        if (after.ts or 0) - (before.ts or 0) < previous_ttl(before):
            # the cache was still warm when the user came back: the compaction only cost money
            warm += 1
            warm_cost += cost
            continue
        # the cache had expired: without the compaction the old context is rewritten cold, with it the
        # next request writes what it actually wrote (observed) after paying for the compaction call
        w = write_price_per_token(after)
        saving += c.pre * w - (cost + after.cw * w)
    return {
        "count": len(idle),
        "estimated": True,
        "note": "Compaction calls are not in the transcript. Cost is estimated as the pre-compaction tokens "
                "read from cache plus a summary of about post-compaction tokens at the output price. A saving is "
                "credited only when the next request came after the cache TTL had run out, and then "
                "compares rewriting the old context cold with the cache write that request really made.",
        "compaction_cost_estimate": _r(comp_cost, 4),
        "followed_by_request": followed,
        "never_followed": never,
        "never_followed_cost_estimate": _r(never_cost, 4),
        "cache_still_warm": warm,
        "cache_still_warm_cost_estimate": _r(warm_cost, 4),
        "without_token_counts": unknown,
        "estimated_saving": _r(saving, 4),
    }


def _read_ratio(rs):
    """Cache-read price over input price, weighted by cache-write tokens (all tokens if none were written)."""
    num = den = 0.0
    for r in rs:
        if not r.price_in:
            continue
        w = r.cw or 0
        num += w * (r.price_cr / r.price_in)
        den += w
    if den:
        return num / den
    for r in rs:
        if r.price_in:
            num += r.pt * (r.price_cr / r.price_in)
            den += r.pt
    return num / den if den else 0.05


def _one_hour(reqs, rebuilds):
    """Would a 1-hour cache for subagents pay off? The dollar counterfactual follows rebuilds.py."""
    rb_by_req = {id(b.req): b for b in rebuilds}
    out = collections.OrderedDict()
    tot_extra = tot_saved = 0.0
    for kind in SUB_KINDS:
        rs = [r for r in reqs if r.kind == kind]
        extra = saved = 0.0
        expired_5_60 = 0
        for r in rs:
            extra += r.cw5m * (pricing.WRITE_1H - pricing.WRITE_5M) * r.price_in
            p = r.prev
            if p is not None:
                gap = (r.ts or 0) - (p.ts or 0)
                if TTL_5M <= gap < TTL_1H and post_rule(r):
                    rebuilt = r.cw - max(0, r.pt - p.pt)
                    saved += rebuilt * (pricing.WRITE_1H * r.price_in - r.price_cr)
            b = rb_by_req.get(id(r))
            if b is not None and b.cause == "expired" and b.bucket == "5-60m":
                expired_5_60 += b.rewritten
        cw_total = sum(r.cw for r in rs)
        share = expired_5_60 / cw_total if cw_total else 0.0
        ratio = _read_ratio(rs)
        be = breakeven(ratio)
        if len(rs) < MIN_VERDICT_REQUESTS:
            verdict = "too little data (<%d requests)" % MIN_VERDICT_REQUESTS
        elif share > be:
            verdict = "would pay off"
        else:
            verdict = "would not pay off"
        out[kind] = {
            "requests": len(rs), "cache_write_tokens": cw_total, "expired_rebuild_tokens_5_60m": expired_5_60,
            "r": _r(share, 4), "read_ratio": _r(ratio, 4), "break_even": _r(be, 4), "verdict": verdict,
            "extra_write_premium": _r(extra, 4), "rebuilds_avoided": _r(saved, 4), "net": _r(saved - extra, 4),
        }
        tot_extra += extra
        tot_saved += saved
    out["all_subagents"] = {"extra_write_premium": _r(tot_extra, 4), "rebuilds_avoided": _r(tot_saved, 4),
                            "net": _r(tot_saved - tot_extra, 4)}
    return out


def _try_this(rep, total, reqs, rebuilds):
    lines = []
    if total:
        tool_wait = sum(b.write_cost for b in rebuilds
                        if b.req.is_sub and b.cause == "expired" and b.waiting.startswith("tool:"))
        share = tool_wait / total
        if share >= TRY_TOOL_WAIT_SHARE:
            lines.append("Subagent rebuilds after tool waits are %.0f%% of usage: run long commands outside "
                         "subagents. %s" % (100 * share, POST_URL))
        for kind in SUB_KINDS:
            v = rep["one_hour_subagent_cache"][kind]
            if v["requests"] >= MIN_VERDICT_REQUESTS and v["r"] > v["break_even"]:
                lines.append("%s rebuilds are %.0f%% of their cache writes, above the %.1f%% break-even: set "
                             "subagentPromptCacheTtl, or experimental.cacheTtl: 1h in that agent's frontmatter. %s"
                             % (kind.capitalize(), 100 * v["r"], 100 * v["break_even"], POST_URL))
        big = next((b for b in rep["context_size"] if b["bucket"] == ">=400K"), None)
        if big and big["pct"] >= 100 * TRY_BIG_CONTEXT_SHARE:
            lines.append("Contexts over 400K are %.0f%% of usage: prefer smaller, disposable contexts, and set "
                         "autoCompactWindow for subagents (Claude Code 2.1.296+). %s" % (big["pct"], POST_URL))
        peak = next((b for b in rep["agent_peak_context"]["rows"] if b["bucket"] == ">=500K"), None)
        if peak and peak["pct"] >= 100 * TRY_BIG_AGENT_SHARE:
            lines.append("%.0f%% of subagent usage came from agents whose context passed 500K: give each agent one "
                         "task, then start a fresh one. %s" % (peak["pct"], POST_URL))
    return lines
