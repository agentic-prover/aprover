"""Normalise bmc-agent run logs written by rich's console handler.

Rich wraps long messages across several physical lines, indenting the
continuations.  Every extractor in this artefact works on logical records,
so all parsing goes through records().
"""
import re, io

LEV = re.compile(
    r"^(?:\[(?P<ts>\d\d/\d\d/\d\d \d\d:\d\d:\d\d)\])?\s*"
    r"(?P<lev>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s{2,}(?P<msg>.*)$")


def records(path):
    """Yield (timestamp, level, message) with continuation lines folded in."""
    cur = None
    ts = None
    for raw in io.open(path, encoding="utf-8", errors="replace"):
        line = raw.rstrip("\n")
        m = LEV.match(line)
        if m:
            if cur is not None:
                yield cur[2], cur[0], re.sub(r"\s+", " ", cur[1]).strip()
            ts = m.group("ts") or ts
            cur = [m.group("lev"), m.group("msg"), ts]
        elif cur is not None and line.startswith(" " * 20):
            cur[1] += " " + line.strip()
        else:
            if cur is not None:
                yield cur[2], cur[0], re.sub(r"\s+", " ", cur[1]).strip()
            cur = None
    if cur is not None:
        yield cur[2], cur[0], re.sub(r"\s+", " ", cur[1]).strip()
