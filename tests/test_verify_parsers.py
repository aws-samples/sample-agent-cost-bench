"""Tests for the verification result parsers (Approach B)."""

from __future__ import annotations

from kirobench.verify.parsers import parse_results


def _write(d, name, text):
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def test_trx_parser(tmp_path):
    _write(tmp_path, "r.trx",
           '<TestRun><ResultSummary>'
           '<Counters total="7" executed="7" passed="6" failed="1" />'
           '</ResultSummary></TestRun>')
    r = parse_results("trx", tmp_path, "", "", 1, None)
    assert (r.passed, r.total) == (6, 7)
    assert abs(r.score - 6 / 7) < 1e-6


def test_junit_xml_parser(tmp_path):
    _write(tmp_path, "TEST-foo.xml",
           '<testsuite tests="9" failures="1" errors="0" skipped="1">'
           '<testcase name="a"/>'
           '<testcase name="b"><failure/></testcase>'
           '</testsuite>')
    r = parse_results("junit-xml", tmp_path, "", "", 1, None)
    # total = tests(9) - skipped(1) = 8 ; passed = 8 - failures(1) - errors(0) = 7
    assert (r.passed, r.total) == (7, 8)
    assert "b" in r.failed_names


def test_pytest_json_parser(tmp_path):
    _write(tmp_path, "report.json", '{"summary": {"passed": 5, "total": 6, "failed": 1}}')
    r = parse_results("pytest-json", tmp_path, "", "", 1, None)
    assert (r.passed, r.total) == (5, 6)


def test_vitest_json_parser(tmp_path):
    _write(tmp_path, "vitest.json", '{"numTotalTests": 10, "numPassedTests": 8}')
    r = parse_results("vitest-json", tmp_path, "", "", 1, None)
    assert (r.passed, r.total) == (8, 10)


def test_tap_parser(tmp_path):
    out = "ok 1 first\nok 2 second\nnot ok 3 third\n"
    r = parse_results("tap", tmp_path, out, "", 1, None)
    assert (r.passed, r.total) == (2, 3)


def test_regex_parser_passed_total(tmp_path):
    out = "Summary: 5 passed, 7 total"
    spec = {"regex": r"(?P<passed>\d+) passed, (?P<total>\d+) total"}
    r = parse_results("regex", tmp_path, out, "", 1, spec)
    assert (r.passed, r.total) == (5, 7)


def test_regex_parser_passed_failed(tmp_path):
    out = "3 passed 2 failed"
    spec = {"regex": r"(?P<passed>\d+) passed (?P<failed>\d+) failed"}
    r = parse_results("regex", tmp_path, out, "", 1, spec)
    assert (r.passed, r.total) == (3, 5)


def test_exit_code_parser(tmp_path):
    ok = parse_results("exit-code", tmp_path, "", "", 0, None)
    assert (ok.passed, ok.total) == (1, 1)
    bad = parse_results("exit-code", tmp_path, "", "", 2, None)
    assert (bad.passed, bad.total) == (0, 1)


def test_missing_results_marks_not_ran(tmp_path):
    r = parse_results("trx", tmp_path, "", "", 1, None)
    assert r.ran is False and r.total == 0 and r.score == 0.0


def test_unknown_parser_raises(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        parse_results("nope", tmp_path, "", "", 0, None)
