"""
Idempotent patcher for /opt/substrate-v2/docker-compose.yml on GENA.

For each target service (v3-atelier, v4-piata, v9-oracle, v8-ambasador), adds
`- BIJOTEL_HMAC_SECRET=${BIJOTEL_HMAC_SECRET}` to its `environment:` block.

Idempotency: skips services that already have BIJOTEL_HMAC_SECRET= line.

Usage:
    python patch_compose.py /opt/substrate-v2/docker-compose.yml

Note: line-based patcher (NOT full YAML re-serialization) -- preserves
comments, anchors (<<: *ecosystem), formatting. Same approach as Pas 5.B.1
lesson learned (yaml.safe_dump destroys comments).
"""
from pathlib import Path
import re
import sys

if len(sys.argv) != 2:
    print("Usage: python patch_compose.py <docker-compose.yml path>", file=sys.stderr)
    sys.exit(1)

compose_path = Path(sys.argv[1])
if not compose_path.exists():
    print(f"ERROR: compose not found: {compose_path}", file=sys.stderr)
    sys.exit(1)

TARGETS = ["v3-atelier", "v4-piata", "v9-oracle", "v8-ambasador"]
ENV_LINE = "      - BIJOTEL_HMAC_SECRET=${BIJOTEL_HMAC_SECRET}"
MARKER_REGEX = re.compile(r"^\s*-\s*BIJOTEL_HMAC_SECRET=")

src = compose_path.read_text(encoding="utf-8")
lines = src.split("\n")

# Find each service block, then find its `environment:` block, then check
# if BIJOTEL_HMAC_SECRET line already present.
result = []
i = 0
patched_services = []
skipped_services = []

while i < len(lines):
    line = lines[i]
    result.append(line)

    # Check if this is a target service header (e.g. `  v3-atelier:`)
    stripped = line.rstrip()
    matched_service = None
    for svc in TARGETS:
        if stripped == f"  {svc}:":
            matched_service = svc
            break

    if matched_service:
        # Walk forward to find `environment:` within this service block
        j = i + 1
        env_idx = None
        while j < len(lines):
            ll = lines[j]
            # New service starts when we hit another `  X:` at 2-space indent
            if re.match(r"^  \S", ll) and not re.match(r"^    ", ll):
                break
            if re.match(r"^    environment:\s*$", ll):
                env_idx = j
                break
            j += 1

        if env_idx is None:
            skipped_services.append((matched_service, "no environment: block"))
            i += 1
            continue

        # Walk from env_idx forward, collect env list lines until block ends
        already_present = False
        last_env_line_idx = env_idx
        k = env_idx + 1
        while k < len(lines):
            ll = lines[k]
            if MARKER_REGEX.match(ll):
                already_present = True
                break
            # env list items start with 6+ spaces and `- `
            if re.match(r"^      -\s", ll):
                last_env_line_idx = k
                k += 1
                continue
            # End of env block (new key at less indent)
            break

        if already_present:
            skipped_services.append((matched_service, "already has BIJOTEL_HMAC_SECRET"))
            i += 1
            continue

        # Insert ENV_LINE after last_env_line_idx
        # We're currently appending lines[i] (service header). Need to walk to
        # last_env_line_idx and insert AFTER. Simpler: append until that line,
        # then insert, then continue.
        # Reset: pop the just-appended service header, walk through with
        # explicit insertion logic.
        result.pop()  # pop service header
        for m in range(i, last_env_line_idx + 1):
            result.append(lines[m])
        result.append(ENV_LINE)
        patched_services.append(matched_service)
        i = last_env_line_idx + 1
        continue

    i += 1

new_src = "\n".join(result)

compose_path.write_text(new_src, encoding="utf-8")
print(f"Patched {compose_path}")
print(f"  Patched services: {patched_services}")
print(f"  Skipped services: {skipped_services}")
print(f"  Old size: {len(src)} bytes")
print(f"  New size: {len(new_src)} bytes")
print(f"  Diff: {len(new_src) - len(src):+d} bytes")
