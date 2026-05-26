"""CLI subcommand handlers — backward-compat re-export shim.

The actual implementations live in ``cli/_helpers.py`` (shared utilities)
and ``cli/cmd_*.py`` (one module per subcommand group). This file existed
as an 870-LOC monolith through v2.5.0; v2.6.0 split it so each new
subcommand doesn't bloat one file further. The shim stays for
backward-compat with downstream callers / tests that do
``from bijotel.cli.commands import _calc_cost`` or
``from bijotel.cli import commands as cmd_mod`` — every previously
importable name is still importable from here.
"""

from __future__ import annotations

from bijotel.cli._helpers import (
    _calc_cost,
    _detect_status,
    _format_time,
    _parse_canonical_body,
    _parse_range_args,
    _resolve_secret,
)
from bijotel.cli.cmd_anchor import anchor_cmd
from bijotel.cli.cmd_archive import archive_cmd
from bijotel.cli.cmd_chain import inspect_cmd, list_cmd, stats_cmd
from bijotel.cli.cmd_energy import (
    _energy_backfill_cmd,
    _energy_summary_cmd,
    energy_cmd,
)
from bijotel.cli.cmd_export import export_cmd
from bijotel.cli.cmd_integrity import integrity_cmd
from bijotel.cli.cmd_keygen import keygen_cmd
from bijotel.cli.cmd_regression import _print_anomalies, regression_cmd
from bijotel.cli.cmd_replay import replay_cmd
from bijotel.cli.cmd_serve import serve_cmd
from bijotel.cli.cmd_verify import (
    verify_cmd,
    verify_continuity_cmd,
    verify_export_cmd,
)

# v2.0.x callers also re-export this constant from inside the file.
from bijotel.processors.export import FORMAT_ID_V2 as FORMAT_ID_V2_NAME

__all__ = [
    # Public handlers
    "anchor_cmd",
    "archive_cmd",
    "energy_cmd",
    "export_cmd",
    "inspect_cmd",
    "integrity_cmd",
    "keygen_cmd",
    "list_cmd",
    "regression_cmd",
    "replay_cmd",
    "serve_cmd",
    "stats_cmd",
    "verify_cmd",
    "verify_continuity_cmd",
    "verify_export_cmd",
    # Private helpers (kept exported because tests import them directly).
    "FORMAT_ID_V2_NAME",
    "_calc_cost",
    "_detect_status",
    "_energy_backfill_cmd",
    "_energy_summary_cmd",
    "_format_time",
    "_parse_canonical_body",
    "_parse_range_args",
    "_print_anomalies",
    "_resolve_secret",
]
