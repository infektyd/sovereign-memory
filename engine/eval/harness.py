"""
Sovereign Memory — Offline Recall Evaluation Harness.

CLI usage:

    Run a comparison across named configs:
        python -m engine.eval.harness run --config baseline,with-expand,with-hyde

    Append a new query to eval/queries.jsonl:
        python -m engine.eval.harness record --query "auth migration" --expected-ids 8412,8413

Reports are written to eval/reports/<timestamp>-<config>.json and a combined
Markdown table to eval/reports/<timestamp>-comparison.md.

Gate rule (documented here and in docs/contracts/WORKFLOWS.md):
    A feature may flip its default only after the harness shows >=+5% recall@5
    on the seed set compared to the baseline config, with no regression on any
    individual query class defined in the queries.jsonl "notes" field.

Supported configs (defined in CONFIGS dict below):
    baseline    — no PR-8 HyDE second pass
    with-expand — {"expand": True} (reserved for PR-7 query expansion)
    with-hyde   — {"use_hyde": True} (reserved for PR-8 HyDE retrieval)

If a config passes an unrecognised kwarg to search(), the harness logs a warning
and proceeds with defaults. This allows config names to be reserved for future
PRs without crashing the harness today.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("sovereign.eval")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# Config definitions
# ---------------------------------------------------------------------------

CONFIGS: Dict[str, Dict[str, Any]] = {
    "baseline": {"use_hyde": False},
    "with-expand": {"expand": True},     # reserved for PR-7 — no-op today
    "with-hyde": {"use_hyde": True},      # PR-8 HyDE cold-query second pass
}

# Known kwargs accepted by RetrievalEngine.retrieve(); anything else is stripped.
_KNOWN_RETRIEVE_KWARGS = {
    "limit", "agent_id", "update_access", "budget_tokens",
    "depth", "include_superseded", "include_rejected", "include_drafts",
    "expand", "use_hyde",
}

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Return the repository root (two levels up from engine/eval/)."""
    return Path(__file__).resolve().parent.parent.parent


def _queries_path() -> Path:
    return _repo_root() / "eval" / "queries.jsonl"


def _reports_dir() -> Path:
    d = _repo_root() / "eval" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Searcher protocol
# ---------------------------------------------------------------------------

class SearcherProtocol:
    """
    Abstract protocol for the object used by the harness.
    The real implementation wraps RetrievalEngine; tests inject a mock.
    """

    def search(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        raise NotImplementedError


class RealSearcher(SearcherProtocol):
    """Wraps engine.retrieval.RetrievalEngine for in-process evaluation."""

    def __init__(self) -> None:
        # Lazy import so the module can be imported without a live DB.
        engine_dir = Path(__file__).resolve().parent.parent
        if str(engine_dir) not in sys.path:
            sys.path.insert(0, str(engine_dir))

        import db as db_mod
        from config import DEFAULT_CONFIG
        from faiss_index import FAISSIndex
        from retrieval import RetrievalEngine

        self._engine = RetrievalEngine(
            db=db_mod.SovereignDB(DEFAULT_CONFIG),
            config=DEFAULT_CONFIG,
            faiss_index=FAISSIndex(DEFAULT_CONFIG),
        )

    def search(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        return self._engine.retrieve(query, **kwargs)


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def _recall_at_k(
    expected_ids: List[int],
    result_doc_ids: List[int],
    k: int,
) -> float:
    """
    Recall@K: fraction of expected doc_ids found in the top-K results.

    recall@K = |relevant ∩ top-K| / |relevant|
    """
    if not expected_ids:
        return 0.0
    top_k = set(result_doc_ids[:k])
    hits = sum(1 for eid in expected_ids if eid in top_k)
    return hits / len(expected_ids)


def _mrr(
    expected_ids: List[int],
    result_doc_ids: List[int],
) -> float:
    """
    Mean Reciprocal Rank: 1/rank of the first relevant result, or 0.
    """
    expected_set = set(expected_ids)
    for rank, did in enumerate(result_doc_ids, start=1):
        if did in expected_set:
            return 1.0 / rank
    return 0.0


def _calibration_error(results: List[Dict[str, Any]], expected_ids: List[int]) -> Optional[float]:
    """
    Cross-encoder calibration error: mean absolute difference between
    a result's reported confidence and whether it was actually relevant.

    Returns None if no results have a confidence field.
    """
    expected_set = set(expected_ids)
    errors = []
    for r in results:
        conf = r.get("confidence")
        if conf is None:
            continue
        did = r.get("doc_id")
        actual = 1.0 if did in expected_set else 0.0
        errors.append(abs(float(conf) - actual))
    if not errors:
        return None
    return sum(errors) / len(errors)


# ---------------------------------------------------------------------------
# Safe search wrapper
# ---------------------------------------------------------------------------

# Track which (config, kwarg) pairs have already been warned about.
_warned_unknown_kwargs: set = set()


def _safe_search(
    searcher: SearcherProtocol,
    query: str,
    config_name: str,
    config_kwargs: Dict[str, Any],
    limit: int = 10,
) -> Tuple[List[Dict[str, Any]], float]:
    """
    Run search() with the given config, stripping unrecognised kwargs with
    a warning (logged once per config/kwarg pair). Returns (results, latency_seconds).
    """
    safe_kwargs: Dict[str, Any] = {"limit": limit, "update_access": False}
    for k, v in config_kwargs.items():
        if k in _KNOWN_RETRIEVE_KWARGS:
            safe_kwargs[k] = v
        else:
            key = (config_name, k)
            if key not in _warned_unknown_kwargs:
                logger.warning(
                    "config %r requested unknown kwarg %r, ignoring", config_name, k
                )
                _warned_unknown_kwargs.add(key)

    t0 = time.perf_counter()
    try:
        results = searcher.search(query, **safe_kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.error("search() raised for config %r query %r: %s", config_name, query, exc)
        results = []
    elapsed = time.perf_counter() - t0
    return results, elapsed


# ---------------------------------------------------------------------------
# Core evaluation loop
# ---------------------------------------------------------------------------

def _extract_doc_ids(results: List[Dict[str, Any]]) -> List[int]:
    """Extract doc_ids from a list of search results."""
    ids = []
    for r in results:
        did = r.get("doc_id") or r.get("provenance", {}) and r.get("provenance", {}).get("doc_id")
        if did is not None:
            ids.append(int(did))
    return ids


def run_eval(
    searcher: SearcherProtocol,
    queries: List[Dict[str, Any]],
    config_name: str,
    config_kwargs: Dict[str, Any],
    ks: Tuple[int, ...] = (1, 3, 5, 10),
) -> Dict[str, Any]:
    """
    Run evaluation over all queries for a single config.

    Returns a report dict with per-query results and aggregate metrics.
    """
    per_query = []
    aggregate_r_at_k = {k: [] for k in ks}
    aggregate_mrr = []
    aggregate_cal_err = []
    total_latency = 0.0

    for q in queries:
        query_text = q["query"]
        expected_ids = [int(i) for i in q.get("expected_doc_ids", [])]
        notes = q.get("notes", "")

        results, latency = _safe_search(
            searcher, query_text, config_name, config_kwargs
        )
        result_ids = _extract_doc_ids(results)

        r_at_k = {k: _recall_at_k(expected_ids, result_ids, k) for k in ks}
        mrr = _mrr(expected_ids, result_ids)
        cal_err = _calibration_error(results, expected_ids)

        for k in ks:
            aggregate_r_at_k[k].append(r_at_k[k])
        aggregate_mrr.append(mrr)
        if cal_err is not None:
            aggregate_cal_err.append(cal_err)
        total_latency += latency

        per_query.append({
            "query": query_text,
            "expected_doc_ids": expected_ids,
            "notes": notes,
            "result_doc_ids": result_ids[:10],
            "recall_at_k": r_at_k,
            "mrr": round(mrr, 4),
            "calibration_error": round(cal_err, 4) if cal_err is not None else None,
            "latency_s": round(latency, 4),
        })

    n = len(queries) or 1
    summary = {
        "config": config_name,
        "n_queries": len(queries),
        "total_latency_s": round(total_latency, 3),
        "mean_latency_s": round(total_latency / n, 4),
        "recall_at_k": {
            k: round(sum(aggregate_r_at_k[k]) / n, 4) for k in ks
        },
        "mrr": round(sum(aggregate_mrr) / n, 4),
        "mean_calibration_error": (
            round(sum(aggregate_cal_err) / len(aggregate_cal_err), 4)
            if aggregate_cal_err else None
        ),
    }

    return {"summary": summary, "per_query": per_query}


# ---------------------------------------------------------------------------
# Loading query set
# ---------------------------------------------------------------------------

def load_queries(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load queries from a JSONL file. Returns list of query dicts."""
    p = path or _queries_path()
    if not p.exists():
        logger.warning("queries.jsonl not found at %s — returning empty set", p)
        return []

    queries = []
    with p.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
                queries.append(obj)
            except json.JSONDecodeError as exc:
                logger.warning("queries.jsonl line %d: JSON parse error: %s", lineno, exc)
    return queries


# ---------------------------------------------------------------------------
# Report serialisation
# ---------------------------------------------------------------------------

def _write_json_report(report: Dict[str, Any], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    logger.info("JSON report written to %s", path)


def _write_markdown_comparison(
    reports: Dict[str, Dict[str, Any]],
    path: Path,
    ks: Tuple[int, ...] = (1, 3, 5, 10),
) -> None:
    """Write a Markdown comparison table across all configs."""
    lines = []
    lines.append(f"# Sovereign Memory — Recall Eval Report")
    lines.append(f"\n**Generated:** {datetime.now(timezone.utc).isoformat()}\n")
    lines.append(f"**Queries:** {next(iter(reports.values()))['summary']['n_queries']}\n")

    # Header row
    k_cols = " | ".join(f"R@{k}" for k in ks)
    lines.append(f"| Config | {k_cols} | MRR | Cal.Err | Latency(s) |")
    lines.append(f"|--------|{'|'.join(['--------'] * len(ks))}|-----|---------|------------|")

    for config_name, report in reports.items():
        s = report["summary"]
        r_cols = " | ".join(
            f"{s['recall_at_k'].get(k, 0.0):.4f}" for k in ks
        )
        cal = s["mean_calibration_error"]
        cal_str = f"{cal:.4f}" if cal is not None else "n/a"
        lines.append(
            f"| {config_name} | {r_cols} | {s['mrr']:.4f} | {cal_str} | {s['mean_latency_s']:.4f} |"
        )

    lines.append("\n## Gate Rule\n")
    lines.append(
        "A feature may flip its default only after the harness shows >=+5% recall@5 "
        "compared to the `baseline` config, with no regression on any individual query class."
    )
    lines.append("")

    with path.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    logger.info("Markdown comparison written to %s", path)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> None:
    """Run evaluation for one or more configs and write reports."""
    config_names = [c.strip() for c in args.config.split(",")]

    # Validate config names
    unknown = [c for c in config_names if c not in CONFIGS]
    if unknown:
        logger.error("Unknown config(s): %s. Available: %s", unknown, list(CONFIGS))
        sys.exit(1)

    queries = load_queries()
    if not queries:
        logger.warning("No queries loaded — producing empty report.")

    # Choose searcher
    if args.mock:
        searcher = _MockSearcher(queries)
    else:
        try:
            searcher = RealSearcher()
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not initialise real searcher: %s", exc)
            logger.info("Re-run with --mock to use deterministic mock searcher")
            sys.exit(1)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    reports: Dict[str, Dict[str, Any]] = {}

    ks = (1, 3, 5, 10)
    for config_name in config_names:
        config_kwargs = CONFIGS[config_name]
        logger.info("Evaluating config: %s", config_name)
        report = run_eval(searcher, queries, config_name, config_kwargs, ks=ks)
        reports[config_name] = report

        # Write per-config JSON
        json_path = _reports_dir() / f"{timestamp}-{config_name}.json"
        _write_json_report(report, json_path)

    # Write combined Markdown
    md_path = _reports_dir() / f"{timestamp}-comparison.md"
    _write_markdown_comparison(reports, md_path, ks=ks)

    # Print summary to stdout
    print(f"\n{'='*60}")
    print(f"Eval complete — {len(queries)} queries, {len(config_names)} config(s)")
    print(f"{'='*60}")
    for config_name, report in reports.items():
        s = report["summary"]
        r5 = s["recall_at_k"].get(5, 0.0)
        print(f"  {config_name:<20} R@5={r5:.4f}  MRR={s['mrr']:.4f}")
    print(f"\nReports: {_reports_dir()}")


def cmd_record(args: argparse.Namespace) -> None:
    """Append a new query entry to eval/queries.jsonl."""
    p = _queries_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    expected_ids = []
    if args.expected_ids:
        expected_ids = [int(i.strip()) for i in args.expected_ids.split(",")]

    entry = {
        "query": args.query,
        "expected_doc_ids": expected_ids,
        "notes": args.notes or "",
    }

    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")

    print(f"Recorded: {entry}")
    print(f"File: {p}")


# ---------------------------------------------------------------------------
# Mock searcher for testing
# ---------------------------------------------------------------------------

class _MockSearcher(SearcherProtocol):
    """
    Deterministic mock searcher.
    For each query, returns results whose doc_ids match the expected_doc_ids
    from the query set. Used for harness self-tests and --mock mode.
    """

    def __init__(self, queries: Optional[List[Dict[str, Any]]] = None) -> None:
        # Build a lookup: query_text -> expected_doc_ids
        self._lookup: Dict[str, List[int]] = {}
        for q in (queries or []):
            self._lookup[q["query"]] = [int(i) for i in q.get("expected_doc_ids", [])]

    def search(self, query: str, limit: int = 10, **kwargs) -> List[Dict[str, Any]]:
        expected = self._lookup.get(query, [])
        results = []
        for rank, did in enumerate(expected[:limit], start=1):
            results.append({
                "doc_id": did,
                "text": f"Mock result for doc {did}",
                "source": f"wiki/mock/{did}.md",
                "heading": "",
                "score": round(1.0 / rank, 4),
                "confidence": round(0.9 / rank, 4),
                "provenance": {"doc_id": did, "backend": "mock"},
                "privacy_level": "safe",
                "recommended_action": "cite",
            })
        return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m engine.eval.harness",
        description="Sovereign Memory offline recall evaluation harness.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run subcommand
    run_p = sub.add_parser("run", help="Run evaluation across configs")
    run_p.add_argument(
        "--config",
        default="baseline",
        help="Comma-separated config names (default: baseline). Available: "
             + ", ".join(CONFIGS),
    )
    run_p.add_argument(
        "--mock",
        action="store_true",
        help="Use the deterministic mock searcher instead of the live engine",
    )

    # record subcommand
    rec_p = sub.add_parser("record", help="Append a query to eval/queries.jsonl")
    rec_p.add_argument("--query", required=True, help="Query string")
    rec_p.add_argument(
        "--expected-ids",
        default="",
        help="Comma-separated expected doc_ids (e.g. 8412,8413)",
    )
    rec_p.add_argument("--notes", default="", help="Optional notes / class label")

    args = parser.parse_args(argv)

    if args.command == "run":
        cmd_run(args)
    elif args.command == "record":
        cmd_record(args)


if __name__ == "__main__":
    main()
