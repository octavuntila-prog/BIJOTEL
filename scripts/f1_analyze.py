"""F1 schema analysis: parsează f1_spans.jsonl și răspunde la Q1-Q4.

Rulează după f1_capture.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def parse_spans(path: Path) -> list[dict]:
    """Parsează ConsoleExporter-style output (pretty JSON, separate cu newline)."""
    text = path.read_text(encoding="utf-8")
    objs = []
    buf = ""
    depth = 0
    in_str = False
    escape = False
    for ch in text:
        buf += ch
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                objs.append(json.loads(buf.strip()))
                buf = ""
    return objs


def main() -> int:
    path = Path("f1_spans.jsonl")
    spans = parse_spans(path)
    print(f"Total spans: {len(spans)}")
    print("=" * 70)

    for i, span in enumerate(spans):
        print(f"\n--- Span {i}: name={span.get('name')!r} ---")
        attrs = span.get("attributes", {})
        prefixes = {}
        for k in attrs:
            prefix = k.split(".", 1)[0]
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
        print(f"Attribute prefixes: {dict(sorted(prefixes.items()))}")
        print(f"All attribute keys ({len(attrs)}):")
        for k in sorted(attrs.keys()):
            v = attrs[k]
            if isinstance(v, str) and len(v) > 80:
                v_repr = f"<str len={len(v)}, prefix={v[:40]!r}...>"
            elif isinstance(v, list):
                v_repr = f"<list len={len(v)}>"
            else:
                v_repr = repr(v)
            print(f"  {k} = {v_repr}")

    # Q1: prefixes summary
    print("\n" + "=" * 70)
    print("Q1 Summary — prefixes across all spans:")
    all_prefixes: dict[str, int] = {}
    for span in spans:
        for k in span.get("attributes", {}):
            p = k.split(".", 1)[0]
            all_prefixes[p] = all_prefixes.get(p, 0) + 1
    for p, n in sorted(all_prefixes.items(), key=lambda x: -x[1]):
        print(f"  {p}.* : {n} occurrences")

    # Q2: nested fields — look at messages-related attrs
    print("\nQ2 Summary — message body fields:")
    for span in spans:
        attrs = span.get("attributes", {})
        msg_keywords = ("prompt", "completion", "messages", "input", "output", "content")
        msg_attrs = {k: v for k, v in attrs.items() if any(kw in k for kw in msg_keywords)}
        if msg_attrs:
            print(f"  span '{span.get('name')}':")
            for k in sorted(msg_attrs.keys()):
                v = msg_attrs[k]
                if isinstance(v, str):
                    print(f"    {k}: <str len={len(v)}>")
                else:
                    print(f"    {k}: <{type(v).__name__}>")

    # Q3: variation per request type
    print("\nQ3 Summary — attribute keys per span:")
    span_attr_sets = []
    for i, span in enumerate(spans):
        attrs = set(span.get("attributes", {}).keys())
        span_attr_sets.append((i, span.get("name"), attrs))
        print(f"  span {i} ({span.get('name')}): {len(attrs)} attrs")
    if len(span_attr_sets) >= 2:
        common = span_attr_sets[0][2]
        for _, _, s in span_attr_sets[1:]:
            common = common & s
        print(f"\n  Common attrs across all {len(span_attr_sets)} spans: {len(common)}")
        for i, name, attrs in span_attr_sets:
            unique = attrs - common
            if unique:
                print(f"  Unique to span {i} ({name}): {sorted(unique)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
