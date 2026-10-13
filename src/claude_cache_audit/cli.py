"""Command line interface."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
import time

from . import __version__, analysis, report, transcripts

DEFAULT_DAYS = 30
_REL = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_when(text, now=None, is_until=False):
    """`YYYY-MM-DD`, an ISO timestamp (local time unless it has an offset) or a relative `7d`/`12h`.

    A bare date as an upper bound covers that whole day.
    """
    now = time.time() if now is None else now
    s = text.strip()
    m = _REL.match(s)
    if m:
        return now - float(m.group(1)) * _UNIT[m.group(2)]
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            d = dt.datetime.strptime(s, "%Y-%m-%d")
            if is_until:
                d += dt.timedelta(days=1)
            return d.timestamp()
        ts = transcripts.parse_ts(s)
        if ts is None:
            raise ValueError
        # naive ISO strings parse as local time already (datetime.timestamp assumes local)
        return ts
    except ValueError:
        raise argparse.ArgumentTypeError("cannot read %r as a date (use YYYY-MM-DD, an ISO timestamp, 7d or 12h)" % text)


def _add_common(p, sub=False):
    d = argparse.SUPPRESS if sub else None
    p.add_argument("--since", metavar="D", default=d, help="start of the window: YYYY-MM-DD, ISO timestamp, 7d, 12h "
                                                       "(default: last %d days)" % DEFAULT_DAYS)
    p.add_argument("--until", metavar="D", default=d, help="end of the window (a bare date includes that day)")
    p.add_argument("--all", action="store_true", default=d if sub else False, help="no default window: everything")
    p.add_argument("--config-dir", metavar="P", action="append", default=d,
                   help="Claude config dir (repeatable). Default: $CLAUDE_CONFIG_DIR, ~/.claude and ~/.claude-*")


def build_parser():
    p = argparse.ArgumentParser(
        prog="claude-cache-audit",
        description="Audit Claude Code's prompt-cache usage from local transcripts. Local only: no network, "
                    "no prompt text read out. Dollar figures are API-equivalent, not your bill.")
    p.add_argument("--version", action="version", version="claude-cache-audit " + __version__)
    _add_common(p)
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="every analysis as JSON (schema_version 1)")
    fmt.add_argument("--share", action="store_true", help="short Markdown block with ratios only, safe to paste")
    subs = p.add_subparsers(dest="command", metavar="{rebuilds}")
    rb = subs.add_parser("rebuilds", help="one line per cache rebuild, costliest first",
                         description="One line per cache rebuild (headline measure), costliest first.")
    rb.add_argument("--top", type=int, default=20, metavar="N", help="how many to show (default 20)")
    _add_common(rb, sub=True)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    now = time.time()
    try:
        since = parse_when(args.since, now) if args.since else None
        until = parse_when(args.until, now, is_until=True) if args.until else None
    except argparse.ArgumentTypeError as e:
        parser.error(str(e))
    if since is None and not args.all and until is None:
        since = now - DEFAULT_DAYS * 86400
    elif since is None and not args.all:
        since = until - DEFAULT_DAYS * 86400

    dirs = args.config_dir
    if dirs:
        dirs = [os.path.expanduser(d) for d in dirs]
    else:
        dirs = transcripts.default_config_dirs()
    searched = list(dirs)
    scan = transcripts.scan_transcripts(
        transcripts.dedupe_dirs(dirs), since=since, progress=transcripts.stderr_progress if sys.stderr.isatty() else _plain_progress)
    if not scan.files and not scan.skipped_old:
        sys.stderr.write("claude-cache-audit: no transcripts found.\nSearched for projects/**/*.jsonl in:\n")
        for d in searched or ["(no config dir found: tried $CLAUDE_CONFIG_DIR, ~/.claude, ~/.claude-*)"]:
            sys.stderr.write("  %s\n" % d)
        sys.stderr.write("Use --config-dir to point at a Claude config directory.\n")
        return 1

    rep, rebuilds = analysis.compute(scan, since=since, until=until, now=now)
    if args.command == "rebuilds":
        out = report.render_rebuilds(rebuilds, max(0, args.top), len(rebuilds))
    elif args.json:
        out = report.render_json(rep)
    elif args.share:
        out = report.render_share(rep)
    else:
        out = report.render_text(rep)
    try:
        sys.stdout.write(out)
        sys.stdout.flush()
    except BrokenPipeError:
        pass
    return 0


def _plain_progress(msg):
    if msg:
        sys.stderr.write(msg + "\n")
