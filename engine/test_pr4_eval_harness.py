"""
PR-4 Tests — Recall eval harness.

Covers:
  4.1  load_queries: parses valid JSONL, skips comments and blank lines
  4.2  load_queries: handles missing file gracefully
  4.3  _recall_at_k: correct computation for K=1,3,5,10
  4.4  _mrr: correct reciprocal rank calculation
  4.5  _calibration_error: computes mean absolute error between confidence and relevance
  4.6  run_eval: end-to-end with deterministic mock searcher
  4.7  run_eval: unknown kwargs in config are stripped with warning (no crash)
  4.8  _write_json_report: produces valid JSON file
  4.9  _write_markdown_comparison: produces .md file with gate rule
  4.10 record mode: appends valid JSONL entry to file
  4.11 _MockSearcher: returns expected doc_ids in order
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make engine directory importable
sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Import the harness under test
# ---------------------------------------------------------------------------

from eval.harness import (
    _MockSearcher,
    _calibration_error,
    _extract_doc_ids,
    _mrr,
    _recall_at_k,
    _write_json_report,
    _write_markdown_comparison,
    load_queries,
    run_eval,
)


# ---------------------------------------------------------------------------
# 4.1  load_queries: parses valid JSONL
# ---------------------------------------------------------------------------

class TestLoadQueries:

    def _write_jsonl(self, path: Path, lines: list) -> None:
        with path.open("w", encoding="utf-8") as fh:
            for line in lines:
                if isinstance(line, dict):
                    fh.write(json.dumps(line) + "\n")
                else:
                    fh.write(line + "\n")

    def test_parses_valid_entries(self, tmp_path):
        p = tmp_path / "queries.jsonl"
        self._write_jsonl(p, [
            {"query": "auth migration", "expected_doc_ids": [1, 2], "notes": "exact-match"},
            {"query": "FAISS cold start", "expected_doc_ids": [3], "notes": "partial-match"},
        ])
        queries = load_queries(p)
        assert len(queries) == 2
        assert queries[0]["query"] == "auth migration"
        assert queries[0]["expected_doc_ids"] == [1, 2]

    def test_skips_comment_lines(self, tmp_path):
        p = tmp_path / "queries.jsonl"
        self._write_jsonl(p, [
            "# this is a comment",
            {"query": "real query", "expected_doc_ids": [10], "notes": "test"},
            "# another comment",
        ])
        queries = load_queries(p)
        assert len(queries) == 1
        assert queries[0]["query"] == "real query"

    def test_skips_blank_lines(self, tmp_path):
        p = tmp_path / "queries.jsonl"
        with p.open("w") as fh:
            fh.write('{"query": "q1", "expected_doc_ids": [1], "notes": ""}\n')
            fh.write("\n")
            fh.write("   \n")
            fh.write('{"query": "q2", "expected_doc_ids": [2], "notes": ""}\n')
        queries = load_queries(p)
        assert len(queries) == 2

    def test_skips_invalid_json_lines_with_warning(self, tmp_path, caplog):
        p = tmp_path / "queries.jsonl"
        with p.open("w") as fh:
            fh.write('{"query": "good", "expected_doc_ids": [1], "notes": ""}\n')
            fh.write("this is not json\n")
        import logging
        with caplog.at_level(logging.WARNING):
            queries = load_queries(p)
        assert len(queries) == 1
        assert any("JSON parse error" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# 4.2  load_queries: handles missing file
# ---------------------------------------------------------------------------

class TestLoadQueriesMissing:

    def test_returns_empty_list_for_missing_file(self, tmp_path, caplog):
        p = tmp_path / "nonexistent.jsonl"
        import logging
        with caplog.at_level(logging.WARNING):
            queries = load_queries(p)
        assert queries == []
        assert any("not found" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# 4.3  _recall_at_k
# ---------------------------------------------------------------------------

class TestRecallAtK:

    def test_perfect_recall(self):
        assert _recall_at_k([1, 2], [1, 2, 3], k=5) == 1.0

    def test_zero_recall(self):
        assert _recall_at_k([1, 2], [3, 4, 5], k=5) == 0.0

    def test_partial_recall(self):
        # 1 of 2 expected found in top-5
        assert _recall_at_k([1, 2], [1, 3, 4, 5, 6], k=5) == 0.5

    def test_k_cutoff_matters(self):
        # doc 2 is at position 4 (index 3), K=3 excludes it
        assert _recall_at_k([2], [1, 3, 4, 2, 5], k=3) == 0.0
        assert _recall_at_k([2], [1, 3, 4, 2, 5], k=4) == 1.0

    def test_empty_expected_returns_zero(self):
        assert _recall_at_k([], [1, 2, 3], k=5) == 0.0

    def test_k_1(self):
        assert _recall_at_k([1], [1, 2, 3], k=1) == 1.0
        assert _recall_at_k([2], [1, 2, 3], k=1) == 0.0


# ---------------------------------------------------------------------------
# 4.4  _mrr
# ---------------------------------------------------------------------------

class TestMRR:

    def test_first_result_relevant(self):
        assert _mrr([1], [1, 2, 3]) == 1.0

    def test_second_result_relevant(self):
        assert _mrr([2], [1, 2, 3]) == 0.5

    def test_third_result_relevant(self):
        assert abs(_mrr([3], [1, 2, 3]) - 1/3) < 1e-6

    def test_no_relevant_result(self):
        assert _mrr([99], [1, 2, 3]) == 0.0

    def test_multiple_expected_first_rank_counts(self):
        # Both [2, 3] expected — rank of first hit (2, rank=2) gives 0.5
        assert _mrr([2, 3], [1, 2, 3]) == 0.5

    def test_empty_expected(self):
        assert _mrr([], [1, 2, 3]) == 0.0


# ---------------------------------------------------------------------------
# 4.5  _calibration_error
# ---------------------------------------------------------------------------

class TestCalibrationError:

    def _make_result(self, doc_id, confidence):
        return {"doc_id": doc_id, "confidence": confidence}

    def test_perfect_calibration_relevant(self):
        results = [self._make_result(1, 1.0)]
        err = _calibration_error(results, expected_ids=[1])
        assert err == pytest.approx(0.0)

    def test_perfect_calibration_irrelevant(self):
        results = [self._make_result(99, 0.0)]
        err = _calibration_error(results, expected_ids=[1])
        assert err == pytest.approx(0.0)

    def test_max_calibration_error(self):
        # Confident=1.0 but irrelevant
        results = [self._make_result(99, 1.0)]
        err = _calibration_error(results, expected_ids=[1])
        assert err == pytest.approx(1.0)

    def test_none_when_no_confidence(self):
        results = [{"doc_id": 1, "text": "no confidence field"}]
        assert _calibration_error(results, expected_ids=[1]) is None

    def test_mean_over_multiple_results(self):
        results = [
            self._make_result(1, 1.0),   # relevant, conf=1.0 → error=0.0
            self._make_result(99, 0.4),  # irrelevant, conf=0.4 → error=0.4
        ]
        err = _calibration_error(results, expected_ids=[1])
        assert err == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# 4.6  run_eval: end-to-end with mock searcher
# ---------------------------------------------------------------------------

class TestRunEval:

    def _make_queries(self):
        return [
            {"query": "auth migration", "expected_doc_ids": [1, 2], "notes": "exact-match"},
            {"query": "FAISS search", "expected_doc_ids": [3], "notes": "partial-match"},
            {"query": "no match query", "expected_doc_ids": [], "notes": "no-match"},
        ]

    def test_run_eval_returns_summary_and_per_query(self):
        queries = self._make_queries()
        mock = _MockSearcher(queries)
        report = run_eval(mock, queries, "baseline", {}, ks=(1, 3, 5, 10))

        assert "summary" in report
        assert "per_query" in report
        assert report["summary"]["n_queries"] == 3
        assert len(report["per_query"]) == 3

    def test_run_eval_perfect_mock_recall(self):
        queries = self._make_queries()
        mock = _MockSearcher(queries)
        report = run_eval(mock, queries, "baseline", {}, ks=(1, 3, 5))

        # For 'auth migration', both expected ids should appear in top-2 results
        per_q = {q["query"]: q for q in report["per_query"]}
        assert per_q["auth migration"]["recall_at_k"][5] == 1.0

    def test_run_eval_no_match_query(self):
        queries = self._make_queries()
        mock = _MockSearcher(queries)
        report = run_eval(mock, queries, "baseline", {}, ks=(5,))

        per_q = {q["query"]: q for q in report["per_query"]}
        # No expected doc_ids → recall is 0 (empty expected is defined as 0)
        assert per_q["no match query"]["recall_at_k"][5] == 0.0

    def test_run_eval_summary_mrr_in_range(self):
        queries = self._make_queries()
        mock = _MockSearcher(queries)
        report = run_eval(mock, queries, "baseline", {}, ks=(5,))
        mrr = report["summary"]["mrr"]
        assert 0.0 <= mrr <= 1.0

    def test_run_eval_empty_queries(self):
        mock = _MockSearcher([])
        report = run_eval(mock, [], "baseline", {}, ks=(5,))
        assert report["summary"]["n_queries"] == 0
        assert report["per_query"] == []


# ---------------------------------------------------------------------------
# 4.7  Unknown kwargs stripped with warning
# ---------------------------------------------------------------------------

class TestUnknownKwargs:

    def test_expand_kwarg_is_forwarded(self, caplog):
        """PR-7: with-expand is now recognised and forwarded to search()."""
        from eval.harness import _safe_search

        queries = [{"query": "test", "expected_doc_ids": [1], "notes": ""}]
        mock = _MockSearcher(queries)

        results, latency = _safe_search(
            mock, "test", "with-expand", {"expand": True}
        )

        assert not any("unknown kwarg" in m for m in caplog.messages)
        assert isinstance(results, list)

    def test_use_hyde_kwarg_passes_through(self, caplog):
        import logging
        from eval.harness import _safe_search

        queries = [{"query": "test", "expected_doc_ids": [1], "notes": ""}]
        mock = _MockSearcher(queries)
        seen = {}
        original_search = mock.search

        def capture_search(query, **kwargs):
            seen.update(kwargs)
            return original_search(query, **kwargs)

        mock.search = capture_search

        with caplog.at_level(logging.WARNING):
            results, latency = _safe_search(
                mock, "test", "with-hyde", {"use_hyde": True}
            )

        assert not any("use_hyde" in m for m in caplog.messages)
        assert seen["use_hyde"] is True
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# 4.8  _write_json_report: produces valid JSON
# ---------------------------------------------------------------------------

class TestWriteJsonReport:

    def test_writes_valid_json(self, tmp_path):
        report = {"summary": {"n_queries": 2, "mrr": 0.75}, "per_query": []}
        p = tmp_path / "report.json"
        _write_json_report(report, p)
        assert p.exists()
        loaded = json.loads(p.read_text())
        assert loaded["summary"]["mrr"] == 0.75

    def test_overwrites_existing_file(self, tmp_path):
        p = tmp_path / "report.json"
        p.write_text("old content")
        report = {"summary": {}, "per_query": []}
        _write_json_report(report, p)
        loaded = json.loads(p.read_text())
        assert "summary" in loaded


# ---------------------------------------------------------------------------
# 4.9  _write_markdown_comparison: has gate rule
# ---------------------------------------------------------------------------

class TestWriteMarkdownComparison:

    def test_produces_markdown_file(self, tmp_path):
        reports = {
            "baseline": {
                "summary": {
                    "config": "baseline",
                    "n_queries": 2,
                    "mrr": 0.5,
                    "recall_at_k": {1: 0.4, 3: 0.5, 5: 0.6, 10: 0.7},
                    "mean_calibration_error": 0.1,
                    "mean_latency_s": 0.05,
                    "total_latency_s": 0.1,
                },
                "per_query": [],
            }
        }
        p = tmp_path / "comparison.md"
        _write_markdown_comparison(reports, p, ks=(1, 3, 5, 10))
        assert p.exists()
        content = p.read_text()
        assert "baseline" in content
        assert "Gate Rule" in content

    def test_gate_rule_mentions_5_percent(self, tmp_path):
        reports = {
            "baseline": {
                "summary": {
                    "config": "baseline",
                    "n_queries": 1,
                    "mrr": 0.5,
                    "recall_at_k": {5: 0.6},
                    "mean_calibration_error": None,
                    "mean_latency_s": 0.01,
                    "total_latency_s": 0.01,
                },
                "per_query": [],
            }
        }
        p = tmp_path / "comparison.md"
        _write_markdown_comparison(reports, p, ks=(5,))
        content = p.read_text()
        assert "+5%" in content or "5%" in content


# ---------------------------------------------------------------------------
# 4.10  record mode: appends valid JSONL entry
# ---------------------------------------------------------------------------

class TestRecordMode:

    def test_record_appends_to_file(self, tmp_path, monkeypatch):
        """cmd_record appends a valid JSONL entry."""
        from eval.harness import cmd_record
        import argparse

        p = tmp_path / "queries.jsonl"
        monkeypatch.setattr("eval.harness._queries_path", lambda: p)

        args = argparse.Namespace(
            query="test query",
            expected_ids="100,200",
            notes="unit-test",
        )
        cmd_record(args)

        assert p.exists()
        line = p.read_text().strip()
        obj = json.loads(line)
        assert obj["query"] == "test query"
        assert obj["expected_doc_ids"] == [100, 200]
        assert obj["notes"] == "unit-test"

    def test_record_appends_multiple_entries(self, tmp_path, monkeypatch):
        from eval.harness import cmd_record
        import argparse

        p = tmp_path / "queries.jsonl"
        monkeypatch.setattr("eval.harness._queries_path", lambda: p)

        for i in range(3):
            cmd_record(argparse.Namespace(
                query=f"query {i}",
                expected_ids=str(i),
                notes="",
            ))

        lines = [l for l in p.read_text().splitlines() if l.strip()]
        assert len(lines) == 3

    def test_record_empty_expected_ids(self, tmp_path, monkeypatch):
        from eval.harness import cmd_record
        import argparse

        p = tmp_path / "queries.jsonl"
        monkeypatch.setattr("eval.harness._queries_path", lambda: p)

        cmd_record(argparse.Namespace(query="q", expected_ids="", notes="no-match"))
        obj = json.loads(p.read_text().strip())
        assert obj["expected_doc_ids"] == []


# ---------------------------------------------------------------------------
# 4.11  _MockSearcher returns expected doc_ids in order
# ---------------------------------------------------------------------------

class TestMockSearcher:

    def test_returns_expected_ids_in_order(self):
        queries = [{"query": "q1", "expected_doc_ids": [10, 20, 30], "notes": ""}]
        mock = _MockSearcher(queries)
        results = mock.search("q1", limit=10)
        ids = [r["doc_id"] for r in results]
        assert ids == [10, 20, 30]

    def test_respects_limit(self):
        queries = [{"query": "q1", "expected_doc_ids": [1, 2, 3, 4, 5], "notes": ""}]
        mock = _MockSearcher(queries)
        results = mock.search("q1", limit=3)
        assert len(results) == 3

    def test_unknown_query_returns_empty(self):
        mock = _MockSearcher([])
        results = mock.search("unknown query")
        assert results == []

    def test_result_has_required_fields(self):
        queries = [{"query": "q1", "expected_doc_ids": [42], "notes": ""}]
        mock = _MockSearcher(queries)
        results = mock.search("q1")
        assert len(results) == 1
        r = results[0]
        assert "doc_id" in r
        assert "text" in r
        assert "score" in r
        assert r["doc_id"] == 42

    def test_scores_decrease_with_rank(self):
        queries = [{"query": "q1", "expected_doc_ids": [1, 2, 3], "notes": ""}]
        mock = _MockSearcher(queries)
        results = mock.search("q1")
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)
