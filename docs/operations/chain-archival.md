# Chain segmentation and archival

The HMAC chain is append-only. Without intervention, ``chain.db`` grows
linearly with traffic — GENA's production chain reached 82 MB at
~6,300 entries (16 days), with full verify around 0.3s. Projected
to 100k entries the DB would be ~1.3 GB; at 500k entries full export
exceeds 6 GB and verify time crosses 20s.

v2.2.0 ships range-aware verify/export and an ``archive`` operation
that peels the oldest entries off into a separate SQLite DB. The main
chain stays small; old segments remain independently verifiable; the
boundary invariant ``archive.last_hmac_hash == main.first_prev_hash``
guarantees no entries can quietly disappear between segments.

---

## Range verify and export

All four range filters work on both ``verify`` and ``export`` and
can be combined::

    bijotel verify --since 2026-05-20
    bijotel verify --until 2026-05-25
    bijotel verify --range 5000:6000
    bijotel verify --last 1000

    bijotel export --range 5000:6000 -o segment.json
    bijotel export --last 100 -o tail.json
    bijotel export --since 2026-05-20 -o week.json --sign-key keys/private.pem

Range exports carry a new ``segment`` block in the JSON. The
verifier uses ``segment.boundary_prev_hash`` as the first-row anchor
instead of GENESIS, so segments roundtrip cleanly:

```json
{
  "format": "bijotel-chain-v1",
  "chain_signature": "...",
  "segment": {
    "first_seq": 5000,
    "last_seq": 6000,
    "total_in_segment": 1001,
    "total_in_full_chain": 6332,
    "boundary_prev_hash": "468871d42e2d76bb...",
    "is_complete_chain": false
  },
  "entries": [...]
}
```

Live numbers on GENA (6,352 entries, 82 MB ``chain.db``):

| Command | Wall time | Result |
|---|---|---|
| `bijotel verify` (full chain) | ~314 ms | `Chain VALID (6352 entries).` |
| `bijotel verify --last 1000` | ~216 ms | `Boundary: prev_hash at seq=5353 = 22dc3f73...` |
| `bijotel verify --range 5000:6000` | ~210 ms | `Chain segment VALID: seq 5000-6000 (1001 entries of 6352 total).` |
| `bijotel export --last 100 -o tail.json` | ~80 ms | 1.3 MB output (vs 64 MB full export) |

---

## Archive

``bijotel archive`` peels the oldest rows into a new SQLite file with
the same schema plus an ``archive_meta`` table holding boundary
metadata::

    bijotel archive \
      --db /data/bijotel_chain.db \
      --output /backup/archive_2026-05.db \
      --before 2026-05-20 \
      --sign-key keys/bijotel_private.pem \
      --dry-run

### `--dry-run` first, always

The dry-run reports exactly what would happen without touching the
source DB:

```
DRY RUN — no changes made.
  Window:        seq 1 → 3783
  first prev:    0000000000000000...
  last hmac:     8649d3be9f145ae7...
  Boundary OK:   next row's prev = 8649d3be9f145ae7... (matches last hmac of archive)
  Source DB remaining: 2569 entries
```

The "Boundary OK" line is the most important — it confirms the
archive's last hmac matches what the row immediately after the
window expects as its ``prev_hash``. If that line is missing or the
hashes diverge, do not proceed.

### Real archive (after dry-run looks right)

Drop `--dry-run` and the operation actually runs::

1. Build the archive DB and copy the matching rows.
2. Write ``archive_meta`` with first_seq / last_seq / first_prev_hash /
   last_hmac_hash / archived_at / source_db / boundary_next_prev_hash.
3. ``verify_chain`` against the freshly built archive — must return
   `VALID` before anything is deleted from the source.
4. Optional: produce the signed JSON sidecar
   (``archive.json`` next to ``archive.db`` when ``--sign-key`` is
   supplied).
5. In a single transaction on the source DB:
   ``DELETE FROM chain WHERE timestamp_ns < before_ns`` (or
   ``seq < before_seq``).
6. ``VACUUM`` the source DB to reclaim disk space.
7. Re-verify the source DB with the trim-aware default (auto-anchors
   to the new MIN(seq)). Must return `VALID`.

If any verify step fails the operation aborts and the source DB is
left exactly as it was — archives are built fully before any DELETE
runs.

### Filter modes

- ``--before YYYY-MM-DD`` — archive entries with
  ``timestamp_ns < midnight(date) UTC``. Most operational use case.
- ``--before-seq N`` — archive entries with ``seq < N``. Useful when
  you want a clean cut at a known boundary (e.g. exactly after a
  secret rotation).

Exactly one of the two is required.

### Trim-aware verify

After an archive, the main chain no longer starts at ``seq=1``.
v2.2.0's ``verify_chain`` handles this transparently: when called
with no range kwargs and ``seq=1`` is absent from the DB, it shifts
the window to ``MIN(seq)`` and uses that row's stored ``prev_hash``
as the boundary anchor. So ``bijotel verify --db chain.db`` keeps
working through any number of archive operations without forcing the
operator to pass ``--range`` flags.

Passing an explicit ``--range 1:...`` or otherwise specifying
``seq_start=1`` keeps the GENESIS anchor — the caller's explicit
intent always wins.

---

## Continuity across segments

After archiving, you typically have a chain like::

    archives/chain_2026-may10_to_may15.db   # seq    1 — 1500
    archives/chain_2026-may15_to_may20.db   # seq 1501 — 3000
    archives/chain_2026-may20_to_may25.db   # seq 3001 — 4800
    chain.db                                 # seq 4801 — present

``bijotel verify`` on each file confirms internal integrity. To prove
the segments fit together — that no entries were dropped between
files — use ``verify-continuity``::

    bijotel verify-continuity \
      archives/chain_2026-may10_to_may15.db \
      archives/chain_2026-may15_to_may20.db \
      archives/chain_2026-may20_to_may25.db \
      chain.db

Output:

```
Segments:
  archives/chain_2026-may10_to_may15.db — 1500 entries, seq 1-1500 — VALID
  archives/chain_2026-may15_to_may20.db — 1500 entries, seq 1501-3000 — VALID
  archives/chain_2026-may20_to_may25.db — 1800 entries, seq 3001-4800 — VALID
  chain.db                              — 1532 entries, seq 4801-6332 — VALID

Boundaries:
  archives/chain_2026-may10_to_may15.db → archives/chain_2026-may15_to_may20.db: OK
  archives/chain_2026-may15_to_may20.db → archives/chain_2026-may20_to_may25.db: OK
  archives/chain_2026-may20_to_may25.db → chain.db: OK

Full chain: 6332 entries across 4 segments, CONTINUOUS.
```

Each boundary check compares the previous segment's
``last_hmac_hash`` to the next segment's first row's ``prev_hash``.
The check uses ``archive_meta`` when available (faster, survives
schema migrations) and falls back to reading the chain rows
directly.

If continuity fails, the output identifies the exact pair::

```
Boundaries:
  archives/seg_A.db → archives/seg_B.db: BREAK
    expected (prev segment's last hmac): 22dc3f73fcb5805522dc3f73fcb58055...
    actual   (next segment's first prev): ff000000000000000000000000000000...
```

This is the strongest cross-segment invariant the tool offers. An
auditor receiving the full set of segment DBs + their archive_meta
can confirm "no entries between segments" without holding the HMAC
secret, by comparing hashes alone.

---

## Recommended cadence

For typical production deployments (~200–500 sealed spans/day),
archive monthly. Keep the last 30 days in the active ``chain.db``,
archive everything older. With Ed25519 signing on each archive, you
build a tamper-evident timeline an auditor can verify segment by
segment with only the public key.

For high-volume deployments (>5,000 spans/day), archive weekly.
SQLite handles this fine — there's no ceiling we've hit on
multi-thousand-entry archives — but verify time grows linearly and
keeping the active window short is worth the cron job.

For research/lab chains where retention requirements are minimal,
the archive operation is also a clean way to publish *just* the
interesting time window (e.g. a paper's experiment) as a signed
artifact, while the operational chain continues to grow elsewhere.

---

## Pitfalls

- **Archive ordering matters for continuity.** Pass DBs to
  ``verify-continuity`` in chronological order (oldest first).
  Out-of-order arguments will report BREAK at the first pair even
  though the data itself is fine.

- **Don't archive into a path that already exists.** The command
  refuses with `FileExistsError` rather than overwriting. Delete or
  rename the old file first if you really mean to reuse the name.

- **Archived rows are deleted from source by design.** This is the
  point of the operation. If you want to keep a full copy AND a
  trimmed source, run ``bijotel export`` (range mode) first, then
  archive.

- **HMAC secret rotation doesn't interact with archives.** Archives
  preserve the historical secret context — the rows inside still
  verify under the secret they were sealed with. Continuity check
  is hash-based and doesn't need the secret at all.
