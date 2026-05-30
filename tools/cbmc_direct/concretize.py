#!/usr/bin/env python3
"""Concretize a CBMC --json-ui counterexample into a real input file.

Entry-point harnesses model the fuzz input as a byte array `data[MAXLEN]`
plus a length `size`. CBMC's failing trace assigns concrete values to those
input variables; we reconstruct the exact bytes the real program would see
and write them to a file. That file is then replayed through the ASan build
— a crash there is a confirmed, reproducible bug.

Usage:
  cbmc <harness> <srcs...> --json-ui --trace ... > cbmc.json
  concretize.py cbmc.json --array data --size-var size --max 8 -o input.bin

Picks the FIRST failing property's trace by default; --property NAME selects
a specific one. Exits 0 and writes the file if a violation trace was found;
exits 3 if the program verified (no counterexample).
"""
import argparse, json, re, sys
from pathlib import Path


def load_cbmc_json(path: Path):
    raw = path.read_text()
    # CBMC --json-ui emits a single JSON array of message objects.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Tolerate trailing noise: grab the outermost [ ... ].
        i, j = raw.find("["), raw.rfind("]")
        if i >= 0 and j > i:
            return json.loads(raw[i:j + 1])
        raise


def iter_failures(doc):
    """Yield (property_name, description, trace) for each FAILURE."""
    for msg in doc:
        if not isinstance(msg, dict):
            continue
        result = msg.get("result")
        if not isinstance(result, list):
            continue
        for prop in result:
            if isinstance(prop, dict) and prop.get("status") == "FAILURE":
                yield (prop.get("property", "?"),
                       prop.get("description", ""),
                       prop.get("trace", []))


def _int_from_value(value):
    """Extract an integer from a CBMC trace 'value' object."""
    if value is None:
        return None
    if isinstance(value, dict):
        for k in ("data", "binary"):
            if k in value:
                v = value[k]
                try:
                    return int(v, 2) if k == "binary" else int(v)
                except (ValueError, TypeError):
                    try:
                        return int(v, 0)
                    except (ValueError, TypeError):
                        return None
    if isinstance(value, (int, str)):
        try:
            return int(value, 0) if isinstance(value, str) else value
        except ValueError:
            return None
    return None


def reconstruct_input(trace, array_name, size_var, maxlen):
    """Walk the trace; return (bytes, size). Last assignment wins."""
    idx_re = re.compile(r"^%s\[\[?(\d+)\]?\]$" % re.escape(array_name))
    buf = [0] * maxlen
    size = maxlen
    seen_any = False
    for step in trace:
        if not isinstance(step, dict) or step.get("stepType") != "assignment":
            continue
        lhs = step.get("lhs", "")
        val = _int_from_value(step.get("value"))
        if lhs == size_var and val is not None:
            size = max(0, min(maxlen, val))
            continue
        m = idx_re.match(lhs)
        if m and val is not None:
            i = int(m.group(1))
            if 0 <= i < maxlen:
                buf[i] = val & 0xFF
                seen_any = True
    return bytes(buf[:size]), seen_any


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cbmc_json", type=Path)
    ap.add_argument("--array", default="data")
    ap.add_argument("--size-var", default="size")
    ap.add_argument("--max", type=int, default=8)
    ap.add_argument("--property", default=None)
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args()

    doc = load_cbmc_json(args.cbmc_json)
    failures = list(iter_failures(doc))
    if not failures:
        print("[concretize] no FAILURE trace (program verified or errored)", file=sys.stderr)
        return 3

    chosen = None
    if args.property:
        for name, desc, tr in failures:
            if name == args.property:
                chosen = (name, desc, tr); break
    if chosen is None:
        chosen = failures[0]
    name, desc, tr = chosen

    data, seen = reconstruct_input(tr, args.array, args.size_var, args.max)
    args.out.write_bytes(data)
    print(f"[concretize] property={name}")
    print(f"[concretize] description={desc}")
    print(f"[concretize] wrote {len(data)} bytes -> {args.out} "
          f"(input-byte assignments found: {seen})")
    print(f"[concretize] hex={data.hex()}")
    print(f"[concretize] total failing properties in trace: {len(failures)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
