"""Parquet observations (source of truth), DuckDB view, failed-HTML snapshots, run state."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bfp.config import Paths
from bfp.sanitize import sanitize_html
from bfp.schedule import Slot, slot_dir

PRICE = pa.decimal128(10, 2)
PCT = pa.decimal128(5, 2)

SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("slot", pa.string()),
        ("ts", pa.timestamp("us", tz="UTC")),
        ("shop", pa.string()),
        ("url", pa.string()),
        ("final_url", pa.string()),
        ("category", pa.string()),
        ("ean", pa.string()),
        ("page_gtin", pa.string()),
        ("sku", pa.string()),
        ("title", pa.string()),
        ("price", PRICE),
        ("currency", pa.string()),
        ("displayed_prev_price", PRICE),
        ("displayed_rrp_price", PRICE),
        ("displayed_low30_price", PRICE),
        ("displayed_discount_pct", PCT),
        ("availability", pa.string()),
        ("parse_method", pa.string()),
        ("display_method", pa.string()),
        ("status", pa.string()),
        ("http_status", pa.int16()),
        ("rendered", pa.bool_()),
        ("elapsed_ms", pa.int32()),
        ("error", pa.string()),
        ("html_snapshot", pa.string()),
        ("collector_version", pa.string()),
    ]
)
COLUMNS = SCHEMA.names
STATUSES = (
    "ok", "parse_failed", "needs_js", "http_error", "not_found", "blocked",
    "robots_disallowed", "network_error", "render_error", "skipped_blocked",
)


def _q(v: Decimal | None, places: str = "0.01") -> Decimal | None:
    return None if v is None else Decimal(v).quantize(Decimal(places))


def write_observations(paths: Paths, slot: Slot, runner: str, shop: str, rows: list[dict]) -> Path:
    """One file per (slot, runner, shop); atomic rename so a crash never leaves half a file."""
    out_dir = slot_dir(paths.observations, slot)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slot.file_prefix}__{runner}__{shop}.parquet"
    cols: dict[str, list] = {c: [] for c in COLUMNS}
    for r in rows:
        for c in COLUMNS:
            v = r.get(c)
            if c in ("price", "displayed_prev_price", "displayed_rrp_price", "displayed_low30_price", "displayed_discount_pct"):
                v = _q(v)
            cols[c].append(v)
    table = pa.Table.from_pydict(cols, schema=SCHEMA)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)
    return path


def observation_files(paths: Paths) -> list[Path]:
    return sorted(paths.observations.glob("date=*/*.parquet"))


def connect(paths: Paths, database: str = ":memory:") -> duckdb.DuckDBPyConnection:
    """DuckDB with an `observations` view over all Parquet files (empty table if none yet)."""
    con = duckdb.connect(database)
    con.execute("SET TimeZone = 'UTC'")
    if observation_files(paths):
        glob = (paths.observations / "date=*" / "*.parquet").as_posix().replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE VIEW observations AS SELECT * EXCLUDE (date), CAST(date AS DATE) AS obs_date "
            f"FROM read_parquet('{glob}', hive_partitioning = true, union_by_name = true)"
        )
    else:
        empty = pa.Table.from_pylist([], schema=SCHEMA)
        con.register("_empty_obs", empty)
        con.execute("CREATE OR REPLACE VIEW observations AS SELECT *, CAST(NULL AS DATE) AS obs_date FROM _empty_obs")
    return con


def save_failed_html(paths: Paths, slot: Slot, shop: str, url: str, html: str) -> str:
    """Sanitised, gzipped snapshot; returns path relative to the project root."""
    h = hashlib.sha1(url.encode()).hexdigest()[:12]
    d = paths.failed_html / slot.local_date.isoformat() / shop
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{slot.file_prefix}_{h}.html.gz"
    meta = f"<!-- bfp snapshot url={url} saved={datetime.now(timezone.utc).isoformat(timespec='seconds')} -->\n"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(meta + sanitize_html(html))
    return path.relative_to(paths.root).as_posix()


# ---- run state -------------------------------------------------------------------------

def blocked_path(paths: Paths) -> Path:
    return paths.state / "blocked.json"


def load_blocked(paths: Paths) -> dict[str, dict]:
    p = blocked_path(paths)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8") or "{}")


def mark_blocked(paths: Paths, shop: str, run_id: str, reason: str) -> None:
    data = load_blocked(paths)
    data.setdefault(shop, {"since": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": run_id, "reason": reason})
    paths.state.mkdir(parents=True, exist_ok=True)
    blocked_path(paths).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def unblock(paths: Paths, shop: str) -> bool:
    data = load_blocked(paths)
    if shop not in data:
        return False
    del data[shop]
    blocked_path(paths).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def write_manifest(paths: Paths, slot: Slot, runner: str, manifest: dict) -> Path:
    paths.health.mkdir(parents=True, exist_ok=True)
    p = paths.health / f"{slot.label}__{runner}.json"
    p.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return p


def load_manifests(paths: Paths) -> list[dict]:
    out = []
    for p in sorted(paths.health.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return sorted(out, key=lambda m: m.get("started_at", ""))
