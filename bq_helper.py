"""BigQuery query helper — run read-only, byte-capped queries and (optionally)
cache the result as Parquet for reuse.

This file lives at the folder root. Notebooks in `notebooks/` import it after adding
the root to the path:

    import sys, os
    sys.path.insert(0, os.path.abspath(".."))
    from bq_helper import run, dry_run_bytes, PROJECT

    # one-off query, nothing written to disk:
    df = run("SELECT 1 AS x")

    # persistent local copy — saved to parquet/arpu.parquet, reused next time:
    df = run(sql, name="arpu", persist=True)

    # same name, but force a fresh query that overwrites the file:
    df = run(sql, name="arpu", persist=True, force=True)

Auth is your own Google identity via Application Default Credentials. It pins the
verified project and byte cap you bound in Mirror Connect (~/.mirror/session.json)
when the notebook kernel starts. BQ_PROJECT may only repeat that confirmed project;
BQ_MAX_GIB and function arguments may lower, but never raise, the accepted cap.

Every query is checked read-only (SELECT-only, refused otherwise) and hard-capped by
`maximum_bytes_billed`, so a non-SELECT is refused and a runaway scan is rejected by
BigQuery *before* it bills.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import google.auth
import pandas as pd
from google.cloud import bigquery

# --- Resolve a verified project + byte cap from Mirror's connection session -------


def _mirror_session() -> dict:
    """The project/cap the user bound in Mirror Connect, if any. Never raises."""
    home = os.environ.get("MIRROR_HOME") or os.path.join(Path.home(), ".mirror")
    try:
        path = Path(home, "session.json")
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            return {}
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


_SESSION = _mirror_session()

#: Active BigQuery project, explicitly confirmed in Mirror Connect.
PROJECT: str | None = _SESSION.get("project") or None
_ENV_PROJECT = os.environ.get("BQ_PROJECT") or None
if _ENV_PROJECT and _ENV_PROJECT != PROJECT:
    raise RuntimeError(
        f"BQ_PROJECT={_ENV_PROJECT!r} is not the user-confirmed Mirror project "
        f"{PROJECT!r}; pick it in Mirror Connect first"
    )
if not PROJECT:
    raise RuntimeError("No verified Mirror project is bound. Open Mirror Connect and pick one.")


def _configured_cap(value) -> float:
    try:
        cap = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("BQ_MAX_GIB must be a positive finite number") from exc
    if not math.isfinite(cap) or not 0 < cap <= 1024:
        raise ValueError("BQ_MAX_GIB must be greater than zero and at most 1024")
    return cap


#: User-accepted per-query ceiling (GiB), shared with CLI and MCP.
MAX_GIB: float = _configured_cap(_SESSION.get("max_gib") or 5.0)
DEFAULT_GIB: float = _configured_cap(os.environ.get("BQ_MAX_GIB") or MAX_GIB)
if DEFAULT_GIB > MAX_GIB:
    raise ValueError(
        f"BQ_MAX_GIB={DEFAULT_GIB:g} exceeds the user-accepted Mirror cap {MAX_GIB:g}"
    )

#: Where persisted query results live, next to this file.
PARQUET_DIR = Path(__file__).parent / "parquet"

_creds, _adc_project = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
_service_account = getattr(_creds, "service_account_email", None)
if _service_account and _service_account != "default":
    raise RuntimeError(
        "bq_helper requires user Application Default Credentials; "
        f"service-account credentials for {_service_account!r} were refused"
    )


def _credential_fingerprint(creds) -> str | None:
    refresh_token = getattr(creds, "refresh_token", None)
    client_id = getattr(creds, "client_id", None)
    if not isinstance(refresh_token, str) or not refresh_token:
        return None
    material = json.dumps(
        {
            "kind": f"{type(creds).__module__}.{type(creds).__qualname__}",
            "client_id": client_id if isinstance(client_id, str) else "",
            "refresh_token": refresh_token,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "adc-v1:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


_ACTIVE_FINGERPRINT = _credential_fingerprint(_creds)
_BOUND_FINGERPRINT = _SESSION.get("credential_fingerprint")
if not _ACTIVE_FINGERPRINT or _ACTIVE_FINGERPRINT != _BOUND_FINGERPRINT:
    raise RuntimeError(
        "The active Google credentials do not match the account that confirmed "
        "this Mirror project. Open Mirror Connect and pick the project again."
    )
#: BigQuery client running as your own identity. Reads are gated to SELECT-only in
#: `run()` (see `_assert_readonly`); every query is byte-capped.
client = bigquery.Client(project=PROJECT, credentials=_creds)


def _live_ceiling() -> float:
    """Re-read lowering/account/project changes made while a kernel is running."""
    live = _mirror_session()
    if (live.get("project") != PROJECT
            or live.get("credential_fingerprint") != _BOUND_FINGERPRINT):
        raise RuntimeError(
            "Mirror's account/project binding changed. Restart this notebook kernel "
            "to use the new confirmed connection."
        )
    return min(MAX_GIB, _configured_cap(live.get("max_gib") or MAX_GIB))


def _cap_bytes(gib: float | None) -> int:
    ceiling = _live_ceiling()
    cap = min(DEFAULT_GIB, ceiling) if gib is None else _configured_cap(gib)
    if cap > ceiling:
        raise ValueError(
            f"requested cap {cap:g} GiB exceeds the configured ceiling {ceiling:g} GiB"
        )
    return int(cap * 1024**3)


def _format_bytes(value: int) -> str:
    """Human-readable binary size without rounding a small cap/scan down to zero."""
    size = int(value or 0)
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GiB"
    if size >= 1024**2:
        return f"{size / 1024**2:.2f} MiB"
    if size >= 1024:
        return f"{size / 1024:.2f} KiB"
    return f"{size} bytes"


_CACHE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _cache_path(name: str | None) -> Path | None:
    if name is None:
        return None
    if not isinstance(name, str) or not _CACHE_NAME.fullmatch(name) or name in {".", ".."}:
        raise ValueError(
            "cache name must be 1-128 letters, numbers, dots, underscores, or hyphens"
        )
    path = PARQUET_DIR / f"{name}.parquet"
    if not path.resolve().is_relative_to(PARQUET_DIR.resolve()):
        raise ValueError("cache path escapes the parquet directory")
    return path


def _cache_fingerprint(sql: str) -> str:
    payload = json.dumps(
        {"project": PROJECT, "sql": sql},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _metadata_path(path: Path) -> Path:
    return path.with_suffix(".meta.json")


def _atomic_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fd = -1
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _cache_matches(path: Path, sql: str) -> bool:
    meta = _metadata_path(path)
    if path.is_symlink() or meta.is_symlink() or not meta.is_file():
        return False
    try:
        if meta.stat().st_size > 1024 * 1024:
            return False
        doc = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("fingerprint") == _cache_fingerprint(sql)


# Read-only by policy, not just by IAM: refuse anything that isn't a SELECT before
# it runs. A cheap lexical check (must start with SELECT/WITH), then BigQuery's own
# statement-type classification via a free dry run as defense-in-depth (catches a
# WITH-prefixed write). Same posture as Mirror's guarded MCP tools.
_FIRST_WORD = re.compile(r"[A-Za-z_]+")
_QUALIFIED_ROUTINE = re.compile(
    r"(?:`[^`]+`|[A-Za-z_][A-Za-z0-9_-]*)"
    r"(?:\s*\.\s*(?:`[^`]+`|[A-Za-z_][A-Za-z0-9_-]*))+\s*\(",
    re.IGNORECASE,
)
_BACKTICK_ROUTINE = re.compile(r"`[^`]+`\s*\(")


def _is_raw_string(sql: str, start: int) -> bool:
    """True if the quote at `start` opens a raw string (`r'..'`, `rb'..'`, `br'..'`)."""
    i = start - 1
    seen = ""
    while i >= 0 and len(seen) < 2 and sql[i] in "rRbB":
        seen = sql[i] + seen
        i -= 1
    if not seen or i >= 0 and (sql[i].isalnum() or sql[i] == "_"):
        return False
    return "r" in seen.lower()


def _literal_end(sql: str, start: int) -> int:
    """Index just past the quoted literal that starts at `start`.

    Handles the GoogleSQL forms the guard can trip over: `'`/`"` strings (including
    triple-quoted and raw), and backtick-quoted identifiers. An unterminated
    single-line string ends at the newline rather than swallowing the rest of the
    query — under-running a literal only risks a false positive, while over-running
    one would hide real code from the guard.
    """
    quote = sql[start]
    n = len(sql)
    triple = quote != "`" and sql.startswith(quote * 3, start)
    delim = quote * 3 if triple else quote
    escapes = quote != "`" and not _is_raw_string(sql, start)
    i = start + len(delim)
    while i < n:
        if escapes and sql[i] == "\\":
            i += 2
            continue
        if sql.startswith(delim, i):
            return i + len(delim)
        if sql[i] == "\n" and not triple and quote != "`":
            return i
        i += 1
    return n


def _strip_sql_comments(sql: str) -> str:
    """Blank out `--`, `#`, and `/* */` comments, preserving everything else.

    Walks the text instead of using a regex so that comment markers *inside* string
    literals or backtick-quoted identifiers stay put: stripping `'--'` in
    `SELECT '--' || dataset.func(x)` would hide the call that follows it. Comment
    bodies become spaces, so the rest of the query keeps its original offsets.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "#" or sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end < 0 else end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = n if end < 0 else end + 2
        elif ch in "'\"`":
            end = _literal_end(sql, i)
            out.append(sql[i:end])
            i = end
            continue
        else:
            out.append(ch)
            i += 1
            continue
        out.append(" " * (end - i))
        i = end
    return "".join(out)


def _first_keyword(sql: str) -> str:
    w = _FIRST_WORD.search(_strip_sql_comments(sql).lstrip())
    return w.group(0).upper() if w else ""


def _assert_no_persistent_routines(sql: str) -> None:
    """Refuse qualified/table/remote routine calls in the executable SQL.

    Comments are stripped first: a comment that names a file or a backticked column
    followed by `(` is prose, not a call, and used to be rejected.
    """
    executable = _strip_sql_comments(sql)
    if _QUALIFIED_ROUTINE.search(executable) or _BACKTICK_ROUTINE.search(executable):
        raise PermissionError(
            "Read-only: qualified routine/table-function calls are not allowed."
        )


def _assert_readonly(sql: str) -> None:
    if _first_keyword(sql) not in {"SELECT", "WITH"}:
        raise PermissionError("Read-only: only SELECT queries are allowed. Refused.")
    _assert_no_persistent_routines(sql)
    if len(sql) > 1_000_000:
        raise ValueError("SQL is too large (maximum 1,000,000 characters)")
    cfg = bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False, use_legacy_sql=False
    )
    st = (client.query(sql, job_config=cfg).statement_type or "UNKNOWN").upper()
    if st != "SELECT":
        raise PermissionError(f"Read-only: refused a {st} statement. Only SELECT is allowed.")


def run(
    sql: str,
    name: str | None = None,
    *,
    persist: bool = False,
    force: bool = False,
    gib: float | None = None,
    max_rows: int = 100_000,
) -> pd.DataFrame:
    """Run `sql` and return a DataFrame, optionally caching it as Parquet.

    Args:
        sql: the query. Read-only; hard-capped by `maximum_bytes_billed`.
        name: base filename (no extension) for the cache, e.g. "arpu" ->
            parquet/arpu.parquet. Required for any persistence/reuse.
        persist: if True (and `name` given), write the result to parquet/ so it
            survives as persistent local data. If False, nothing is written.
        force: if True, re-run the query and overwrite the cached file even when
            one exists. If False, an existing cache is reused (no query, no cost).
        gib: per-query byte cap. Defaults to the cap bound in Mirror (MAX_GIB).
        max_rows: maximum rows downloaded into memory (1 to 1,000,000).

    Reuse rule: with a `name` and an existing parquet file, the cache is returned
    unless `force=True`. `persist=False` means "don't keep this on disk at all."
    """
    cap = _cap_bytes(gib)
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= 1_000_000:
        raise ValueError("max_rows must be between 1 and 1,000,000")
    path = _cache_path(name)

    # Reuse existing local data unless the caller forces a fresh query.
    if path is not None and path.is_file() and not force and _cache_matches(path, sql):
        print(f"Reusing cached data: {path}")
        return pd.read_parquet(path)
    if path is not None and path.exists() and not force:
        print(f"Ignoring stale or unverified cache: {path}")

    _assert_readonly(sql)  # refuse writes before we run anything
    print(f"Querying BigQuery (cap {_format_bytes(cap)}){f' -> {name}' if name else ''} ...")
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=cap, use_legacy_sql=False
        ),
    )
    rows = job.result(max_results=max_rows)
    df = rows.to_dataframe(create_bqstorage_client=False)
    truncated = getattr(rows, "total_rows", len(df)) > len(df)
    df.attrs["mirror_total_rows"] = getattr(rows, "total_rows", None)
    df.attrs["mirror_truncated"] = truncated
    df.attrs["mirror_project"] = PROJECT
    df.attrs["mirror_job_id"] = getattr(job, "job_id", None)
    df.attrs["mirror_bytes_processed"] = int(job.total_bytes_processed or 0)
    df.attrs["mirror_bytes_billed"] = int(getattr(job, "total_bytes_billed", 0) or 0)
    df.attrs["mirror_cache_hit"] = getattr(job, "cache_hit", None)
    if truncated:
        print(f"  warning: result truncated to {len(df):,} rows (max_rows={max_rows:,})")
    print(f"  {len(df):,} rows, scanned {_format_bytes(job.total_bytes_processed or 0)}")

    # Persist only when explicitly asked (and we have a name to write under).
    if persist and path is not None:
        if truncated:
            raise RuntimeError(
                "refusing to persist an incomplete result; add a SQL LIMIT or raise max_rows"
            )
        PARQUET_DIR.mkdir(exist_ok=True, mode=0o700)
        if os.name != "nt":
            PARQUET_DIR.chmod(0o700)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=PARQUET_DIR
        )
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            df.to_parquet(tmp, index=False)
            if os.name != "nt":
                tmp.chmod(0o600)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        _atomic_text(
            _metadata_path(path),
            json.dumps({
                "fingerprint": _cache_fingerprint(sql),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "project": PROJECT,
                "rows": len(df),
            }, indent=2) + "\n",
        )
        print(f"  saved -> {path}")
    elif persist and path is None:
        print("  persist=True ignored: pass a `name` to save the file.")

    return df


def dry_run_bytes(sql: str) -> int:
    """Bytes the query would scan, without running it. Check cost before a big run."""
    _live_ceiling()
    _assert_readonly(sql)
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, use_legacy_sql=False
        ),
    )
    n = job.total_bytes_processed
    print(f"Would scan {_format_bytes(n)} ({n:,} bytes)")
    return n
