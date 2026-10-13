"""Text, JSON and shareable renderings of the analysis dict."""

from __future__ import annotations

import collections
import datetime as dt
import json

from . import pricing
from .analysis import CAUSES, KINDS, SUB_KINDS

WIDTH = 100
HEADLINE_CAUSES = tuple(c for c in CAUSES if c != "compaction")

CAUSE_LABEL = {
    "compaction": "compaction",
    "model_switch": "model switch",
    "expired": "expired",
    "prefix_change": "prefix change",
}
SHARE_KIND_LABEL = {"subagent": "plain subagents", "teammate": "teammate agents", "fork": "fork agents"}
CAUSE_NOTE = {
    "compaction": "new content, not an avoidable rebuild",
    "model_switch": "different model than the previous request",
    "expired": "gap at least the TTL of the latest cache write",
    "prefix_change": "cache lost with no gap or model change",
}


def money(x):
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 100:
        return sign + "${:,.0f}".format(x)
    return sign + "${:,.2f}".format(x)


def tokens(n):
    if n >= 1e9:
        return "%.2fB" % (n / 1e9)
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%.0fK" % (n / 1e3)
    return str(int(n))


def pct(x):
    if x and abs(x) < 0.1:
        return "<0.1%"
    return "%.1f%%" % x


def _wrap(text, indent="  ", width=WIDTH, hang="    "):
    words, lines, cur = text.split(), [], indent
    for w in words:
        if len(cur) + len(w) + 1 > width and cur.strip():
            lines.append(cur.rstrip())
            cur = hang + w + " "
        else:
            cur += w + " "
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def _wrap_items(label, items, indent="  ", width=WIDTH, hang="    "):
    """`label item, item, ...` wrapped only between items, so an item never splits across lines."""
    lines, cur = [], indent + label
    for i, it in enumerate(items):
        piece = it + ("," if i < len(items) - 1 else "")
        if len(cur) + 1 + len(piece) > width and cur.strip() and cur != indent + label:
            lines.append(cur)
            cur = hang + piece
        else:
            cur += " " + piece
    lines.append(cur)
    return lines


def _table(rows, header, aligns):
    """Aligned plain-text table. rows: list of lists of str."""
    cols = len(header)
    widths = [max(len(str(header[i])), *(len(str(r[i])) for r in rows)) if rows else len(str(header[i]))
              for i in range(cols)]
    out = []

    def fmt(r):
        cells = []
        for i in range(cols):
            s = str(r[i])
            cells.append(s.rjust(widths[i]) if aligns[i] == "r" else s.ljust(widths[i]))
        return "  " + "  ".join(cells).rstrip()

    out.append(fmt(header))
    for r in rows:
        out.append(fmt(r))
    return out


def render_text(rep):
    o = []
    t = rep["totals"]
    w = rep["window"]
    o.append("claude-cache-audit %s" % rep["tool_version"])
    o.append("=" * 40)
    first = (w["first_request"] or "")[:10]
    last = (w["last_request"] or "")[:10]
    o.append("Window:        %s to %s (%s days)" % (first or "-", last or "-", w["days"]))
    o.append("Requests:      {:,}".format(t["requests"]))
    o.append("Transcripts:   {:,} main, {:,} subagent ({:,} files scanned)".format(
        t["transcripts_main"], t["transcripts_subagent"], t["files_scanned"]))
    v = t["claude_code_versions"]
    o.append("Claude Code:   %s" % ("%s to %s" % (v["min"], v["max"]) if v["min"] else "unknown"))
    o.append("Total:         %s  (API-equivalent prices as of %s; not your bill)"
             % (money(t["cost"]), rep["pricing"]["as_of"]))
    if not t["requests"]:
        o.append("")
        o.append("No requests in this window.")
        return "\n".join(o) + "\n"

    # 1 usage split
    o.append("")
    o.append("1. Usage split (API-equivalent)")
    us = rep["usage_split"]
    names = [("cache_read", "cache reads"), ("cache_write_5m", "cache writes, 5m"),
             ("cache_write_1h", "cache writes, 1h"), ("uncached_input", "uncached input"), ("output", "output")]
    rows = [[lab, money(us[k]["cost"]), pct(us[k]["pct"]), tokens(us[k]["tokens"])] for k, lab in names]
    o += _table(rows, ["", "cost", "share", "tokens"], ["l", "r", "r", "r"])
    o.append("  thinking is %s of output tokens" % pct(us["output"]["thinking_pct_of_output_tokens"]))

    # 2 by kind
    o.append("")
    o.append("2. By agent kind")
    rows = [[k, money(d["cost"]), pct(d["pct"]), "{:,}".format(d["requests"]), "{:,}".format(d["transcripts"])]
            for k, d in rep["by_kind"].items()]
    o += _table(rows, ["", "cost", "share", "requests", "transcripts"], ["l", "r", "r", "r", "r"])

    # 3 agents by peak context
    o.append("")
    o.append("3. Subagents by peak context (largest prompt of each agent)")
    ap = rep["agent_peak_context"]
    rows = [[b["bucket"], "{:,}".format(b["agents"]), pct(b["agents_pct"]), money(b["cost"]), pct(b["pct"])]
            for b in ap["rows"]]
    o += _table(rows, ["peak", "agents", "of agents", "cost", "of cost"], ["l", "r", "r", "r", "r"])
    o.append("  an agent is one subagent, teammate or fork transcript; shares are of these agents' cost.")

    # 4 rebuilds
    rb = rep["rebuilds"]
    h = rb["headline"]
    o.append("")
    o.append("4. Cache rebuilds")
    cs = rb["cold_starts"]
    o.append("  Cold starts (first request of a transcript, never a rebuild): {:,}, write cost {}".format(
        cs["count"], money(cs["write_cost"])))
    o.append("  Rebuild premium, headline: %s, %s of total  (%s rebuilds, %s tokens rewritten)" % (
        money(h["premium"]), pct(h["pct_of_total_cost"]), "{:,}".format(h["count"]), tokens(h["rewritten_tokens"])))
    pr = rb["post_rule"]
    o.append("  Cross-check, the post's rule (write >= 20K and >= half the prompt): %s, %s of total" % (
        money(pr["premium"]), pct(pr["pct_of_total_cost"])))
    o.append("")
    rows = []
    for c in HEADLINE_CAUSES:
        d = h["by_cause"][c]
        rows.append([CAUSE_LABEL[c], "{:,}".format(d["count"]), tokens(d["rewritten_tokens"]),
                     money(d["premium"]), pct(d["pct_of_total_cost"])])
    o += _table(rows, ["cause", "rebuilds", "rewritten", "premium", "of total"], ["l", "r", "r", "r", "r"])
    for c in HEADLINE_CAUSES:
        o.append("    %-13s %s" % (CAUSE_LABEL[c], CAUSE_NOTE[c]))
    cmp_ = h["by_cause"]["compaction"]
    o.append("  Compaction, reported apart and not in the headline (%s):" % CAUSE_NOTE["compaction"])
    o.append("    %s rebuilds, %s rewritten, %s, %s of total" % (
        "{:,}".format(cmp_["count"]), tokens(cmp_["rewritten_tokens"]), money(cmp_["premium"]),
        pct(cmp_["pct_of_total_cost"])))
    comp = cmp_.get("by_compaction_kind") or {}
    if comp and cmp_["count"]:
        o.append("    compaction rebuilds by kind: " + ", ".join("%s %d" % (k, n) for k, n in comp.items()))
    o.append("")
    o.append("  Premium by who and gap (cause totals above; full slices in --json):")
    gaps = ["<5m", "5-60m", "1-8h", ">8h"]
    slices = {}
    for s in h["slices"]:
        k = (s["who"], s["gap"])
        slices[k] = slices.get(k, 0) + s["premium"]
    rows = []
    for who in KINDS:
        if any((who, g) in slices for g in gaps):
            rows.append([who] + [money(slices.get((who, g), 0)) for g in gaps])
    o += _table(rows, ["who"] + gaps, ["l", "r", "r", "r", "r"])
    o.append("")
    o.append("  Premium by what the previous request was waiting on (top 5):")
    rows = [[k, "{:,}".format(d["count"]), money(d["premium"])] for k, d in list(h["by_waiting_on"].items())[:5]]
    o += _table(rows, ["waiting on", "rebuilds", "premium"], ["l", "r", "r"])
    tw = h["subagent_tool_waits"][:5]
    if tw:
        o.append("")
        o += _wrap_items("Top tools subagents waited on before a rebuild:", [
            "%s %d (%s)" % (x["tool"], x["count"], money(x["premium"])) for x in tw])

    # 5 hit rate
    o.append("")
    o.append("5. Cache hit rate by idle gap (prompts of 30K+ tokens, same model as the previous request)")
    hr = rep["hit_rate"]
    rows = [[r["who"], r["gap"], "{:,}".format(r["requests"]), "%.1f%%" % r["hit_pct"], "%.1f%%" % r["rebuild_pct"]]
            for r in hr["rows"]]
    o += _table(rows, ["who", "gap", "requests", "hit", "rebuild"], ["l", "l", "r", "r", "r"])
    ttl = hr["cache_write_tokens_by_ttl"]
    o.append("  cache-write tokens by TTL: " + "; ".join(
        "%s 1h %s, 5m %s" % (k, tokens(v["1h"]), tokens(v["5m"])) for k, v in ttl.items()))

    # 6 context size
    o.append("")
    o.append("6. Context size (cost by prompt size)")
    rows = [[b["bucket"], "{:,}".format(b["requests"]), money(b["cost"]), pct(b["pct"])] for b in rep["context_size"]]
    o += _table(rows, ["prompt", "requests", "cost", "share"], ["l", "r", "r", "r"])

    # 7 idle compactions
    o.append("")
    o.append("7. Idle compactions")
    cp = rep["compactions"]
    o.append("  compactions in window: auto %d, manual %d, idle %d" % (cp["auto"], cp["manual"], cp["idle"]))
    ic = rep["idle_compactions"]
    if ic["count"]:
        o.append("  Compaction calls are not logged; their cost is estimated (read the old context, write a summary):")
        o.append("    estimated cost of the %d idle compaction calls: %s" % (
            ic["count"] - ic["without_token_counts"], money(ic["compaction_cost_estimate"])))
        o.append("    followed by another request: %d; never followed (pure cost, %s): %d" % (
            ic["followed_by_request"], money(ic["never_followed_cost_estimate"]), ic["never_followed"]))
        o.append("    next request came before the cache expired (pure cost, %s): %d" % (
            money(ic["cache_still_warm_cost_estimate"]), ic["cache_still_warm"]))
        o.append("    ESTIMATED saving vs rewriting the old context cold, expired cases only: %s" % (
            money(ic["estimated_saving"])))
    else:
        o.append("  none (idle compaction needs Claude Code 2.1.286 or later)")

    # 8 one hour verdict
    o.append("")
    o.append("8. Would a 1-hour subagent cache pay off?")
    oh = rep["one_hour_subagent_cache"]
    rows = []
    for k in SUB_KINDS:
        d = oh[k]
        rows.append([k, "{:,}".format(d["requests"]), pct(100 * d["r"]), pct(100 * d["break_even"]),
                     money(d["extra_write_premium"]), money(d["rebuilds_avoided"]), money(d["net"]), d["verdict"]])
    o += _table(rows, ["kind", "requests", "r", "break-even", "extra", "avoided", "net", "verdict"],
                ["l", "r", "r", "r", "r", "r", "r", "l"])
    a = oh["all_subagents"]
    o.append("  all subagents: extra write premium %s, rebuilds avoided %s, net %s" % (
        money(a["extra_write_premium"]), money(a["rebuilds_avoided"]), money(a["net"])))
    o.append("  r = share of the kind's cache-write tokens that were expired rebuilds after 5-60 minute gaps.")

    # 9 try this
    if rep["try_this"]:
        o.append("")
        o.append("9. Try this")
        for line in rep["try_this"]:
            o += _wrap("- " + line, indent="  ")

    # footer
    o.append("")
    notes = []
    notes.append("malformed lines skipped: {:,}".format(t["malformed_lines"]))
    if t["unknown_models"]:
        notes.append("unknown models priced at $0: " + ", ".join(
            "%s (%d requests)" % (m, n) for m, n in t["unknown_models"].items()))
    if t["write_tokens_without_ttl_split"]:
        notes.append("%s cache-write tokens had no 5m/1h split and were priced as 5m"
                     % tokens(t["write_tokens_without_ttl_split"]))
    if t["files_skipped_old"]:
        notes.append("{:,} older files skipped".format(t["files_skipped_old"]))
    for n in notes:
        o += _wrap("* " + n, indent="  ")
    return "\n".join(o) + "\n"


def render_json(rep):
    return json.dumps(rep, indent=2) + "\n"


def _round_requests(n):
    if n >= 1000:
        k = n / 1000.0
        k = int(round(k, -1)) if k >= 100 else int(round(k))
        return "about {:,} thousand".format(k)
    return "under 1,000" if n else "none"


def render_share(rep):
    """A short Markdown block with ratios and percentages only. No dollars, names, paths or ids."""
    t, h = rep["totals"], rep["rebuilds"]["headline"]
    us, bk = rep["usage_split"], rep["by_kind"]
    v = t["claude_code_versions"]
    o = ["### Claude Code cache audit (claude-cache-audit %s)" % rep["tool_version"], ""]
    o.append("- Window: %s days, %s requests" % (round(rep["window"]["days"]), _round_requests(t["requests"])))
    if v["min"]:
        o.append("- Claude Code versions: %s to %s" % (v["min"], v["max"]))
    mix = collections.OrderedDict()
    for k, x in rep["model_mix"].items():  # a model outside the known families may be a gateway id or ARN
        k = k if k in pricing.FAMILIES else "other"
        mix[k] = mix.get(k, 0.0) + x
    mix = ", ".join("%s %s" % (k, pct(x)) for k, x in sorted(mix.items(), key=lambda kv: -kv[1])[:5])
    if mix:
        o.append("- Model mix by cost: " + mix)
    o.append("- Cost split: cache reads %s, cache writes %s (1h writes %s), uncached input %s, output %s" % (
        pct(us["cache_read"]["pct"]), pct(us["cache_write_5m"]["pct"] + us["cache_write_1h"]["pct"]),
        pct(us["cache_write_1h"]["pct"]), pct(us["uncached_input"]["pct"]), pct(us["output"]["pct"])))
    o.append("- By agent kind: " + ", ".join("%s %s" % (k, pct(bk[k]["pct"])) for k in KINDS))
    o.append("- Rebuild premium: %s of total cost (the post's rule: %s)" % (
        pct(h["pct_of_total_cost"]), pct(rep["rebuilds"]["post_rule"]["pct_of_total_cost"])))
    o.append("- Rebuilds by cause (share of premium): " + ", ".join(
        "%s %s" % (CAUSE_LABEL[c], pct(100.0 * h["by_cause"][c]["premium"] / h["premium"] if h["premium"] else 0))
        for c in HEADLINE_CAUSES))
    cmp_ = h["by_cause"]["compaction"]
    o.append("- Compaction rebuilds, reported apart: %s of total cost" % pct(cmp_["pct_of_total_cost"]))
    big = next((b for b in rep["context_size"] if b["bucket"] == ">=400K"), None)
    if big:
        o.append("- Prompts of 400K tokens or more: %s of cost" % pct(big["pct"]))
    ap = rep["agent_peak_context"]
    if ap["agents"]:
        for label, key in (("Agents", "agents_pct"), ("Subagent cost", "pct")):
            o.append("- %s by peak context: " % label + ", ".join(
                "%s %s" % (b["bucket"], pct(b[key])) for b in ap["rows"]))
    cp = rep["compactions"]
    o.append("- Compactions: %d auto, %d manual, %d idle" % (cp["auto"], cp["manual"], cp["idle"]))
    for k in SUB_KINDS:
        d = rep["one_hour_subagent_cache"][k]
        if d["requests"]:
            o.append("- 1-hour cache, %s: expired-rebuild share of cache writes %s vs break-even %s, %s" % (
                SHARE_KIND_LABEL[k], pct(100 * d["r"]), pct(100 * d["break_even"]), d["verdict"]))
    return "\n".join(o) + "\n"


# ---------------------------------------------------------------------------- rebuilds command

def _gap_text(gap):
    if gap < 3600:
        return "%dm" % round(gap / 60.0)
    return "%.1fh" % (gap / 3600.0)


def _project(r):
    import os

    if r.cwd:
        return os.path.basename(r.cwd.rstrip("/\\")) or r.cwd
    return r.project


def render_rebuilds(rebuilds, top, total_count):
    o = []
    hdr = ["when", "project", "session", "agent", "cause", "gap", "waiting on", "rewritten", "premium"]
    base = []
    for b in rebuilds[:top]:
        r = b.req
        when = dt.datetime.fromtimestamp(r.ts).strftime("%m-%d %H:%M") if r.ts else "-"
        cause = CAUSE_LABEL[b.cause].replace(" ", "-")
        if b.cause == "compaction":
            cause = "cmp:" + (b.compaction_kind or "?")
        base.append([when, _project(r), (r.session or r.owner or "")[:8], r.agent_type, cause,
                     _gap_text(b.gap), b.waiting, tokens(b.rewritten), money(b.premium)])
    # long cells are cut (project 14, agent 10, waiting on 15) and give way more until every line fits
    for k in range(0, 10):
        pl, al, wl = max(7, 14 - k), max(5, 10 - k), max(10, 15 - k)
        rows = [[r[0], r[1][:pl], r[2], r[3][:al], r[4], r[5], r[6][:wl], r[7], r[8]] for r in base]
        lines = _table(rows, hdr, ["l", "l", "l", "l", "l", "r", "l", "r", "r"])
        if max(len(x) for x in lines) <= WIDTH:
            break
    o += lines
    o.append("")
    o.append("  top %d of {:,} rebuilds by premium (headline measure), API-equivalent".format(total_count) % min(top, total_count))
    return "\n".join(o) + "\n"
