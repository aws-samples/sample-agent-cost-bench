"""
Result parsers: turn a test runner's output into (passed, total) so any
language's verification maps to a graduated 0.0–1.0 functional score.

Each parser reads from the results directory (files written by the test command
to ``$RESULTS_DIR``) and/or the captured stdout/stderr, and returns a
``ParseResult``. Register new formats by adding to ``PARSERS``.

Built-in parsers:
  trx           — .NET `dotnet test --logger trx`
  junit-xml     — JUnit/Surefire XML (`TEST-*.xml`, `*.xml`)
  pytest-json   — pytest-json-report (`report.json` with a `summary` block)
  vitest-json   — vitest `--reporter=json --outputFile=...`
  tap           — Test Anything Protocol on stdout
  regex         — custom regex with named groups (passed/total or passed/failed)
  exit-code     — binary: exit 0 => 1/1, else 0/1
  reward-file   — Harbor / Terminal-Bench: a reward.txt or reward.json written by
                  the task's own verifier (a float in [0,1], or an int pass/total
                  pair). Used by imported Terminal-Bench 4.x tasks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET


@dataclass
class ParseResult:
    passed: int = 0
    total: int = 0
    ran: bool = False          # did the suite actually execute / produce results
    detail: str = ""
    failed_names: list[str] = field(default_factory=list)
    # Human-readable per-check summary (e.g. "11/13 checks passed") when the
    # verifier reports individual checks separately from its score.
    checks_detail: str = ""

    @property
    def score(self) -> float:
        return self.passed / self.total if self.total > 0 else 0.0


def _glob_one(results_dir: Path, *patterns: str) -> list[Path]:
    # Patterns can overlap (e.g. "TEST-*.xml" and "*.xml" both match the same
    # file), so dedupe while preserving order to avoid double-counting results.
    out: list[Path] = []
    seen: set[Path] = set()
    for pat in patterns:
        for p in sorted(results_dir.rglob(pat)):
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def parse_trx(results_dir, stdout, stderr, exit_code, spec) -> ParseResult:
    files = _glob_one(results_dir, "*.trx")
    if not files:
        return ParseResult(detail="no .trx results produced (build/compile likely failed)")
    total = passed = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'<Counters\b[^>]*\btotal="(\d+)"', text)
        p = re.search(r'<Counters\b[^>]*\bpassed="(\d+)"', text)
        if m:
            total += int(m.group(1))
        if p:
            passed += int(p.group(1))
    return ParseResult(passed=passed, total=total, ran=total > 0,
                       detail=f"{passed}/{total} tests passed")


def parse_junit_xml(results_dir, stdout, stderr, exit_code, spec) -> ParseResult:
    files = _glob_one(results_dir, "TEST-*.xml", "*.xml")
    if not files:
        return ParseResult(detail="no JUnit XML produced (compile likely failed)")
    tests = failures = errors = skipped = 0
    failed_names: list[str] = []
    for f in files:
        try:
            root = ET.fromstring(f.read_text(encoding="utf-8", errors="replace"))
        except (ET.ParseError, OSError):
            continue
        suites = [root] if root.tag == "testsuite" else root.iter("testsuite")
        for s in suites:
            tests += int(s.get("tests", 0))
            failures += int(s.get("failures", 0))
            errors += int(s.get("errors", 0))
            skipped += int(s.get("skipped", 0))
            for case in s.iter("testcase"):
                if case.find("failure") is not None or case.find("error") is not None:
                    failed_names.append(case.get("name", "?"))
    total = tests - skipped
    passed = total - failures - errors
    passed = max(0, passed)
    return ParseResult(passed=passed, total=total, ran=total > 0,
                       detail=f"{passed}/{total} tests passed", failed_names=failed_names[:20])


def parse_pytest_json(results_dir, stdout, stderr, exit_code, spec) -> ParseResult:
    files = _glob_one(results_dir, "report.json", "*.json")
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        summary = data.get("summary")
        if isinstance(summary, dict) and ("total" in summary or "collected" in summary):
            total = int(summary.get("total", summary.get("collected", 0)))
            passed = int(summary.get("passed", 0))
            return ParseResult(passed=passed, total=total, ran=total > 0,
                               detail=f"{passed}/{total} tests passed")
    return ParseResult(detail="no pytest json report produced")


def parse_vitest_json(results_dir, stdout, stderr, exit_code, spec) -> ParseResult:
    files = _glob_one(results_dir, "vitest.json", "*.json")
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if "numTotalTests" in data:
            total = int(data.get("numTotalTests", 0))
            passed = int(data.get("numPassedTests", 0))
            return ParseResult(passed=passed, total=total, ran=total > 0,
                               detail=f"{passed}/{total} tests passed")
    return ParseResult(detail="no vitest json report produced")


def parse_tap(results_dir, stdout, stderr, exit_code, spec) -> ParseResult:
    text = f"{stdout}\n{stderr}"
    ok = len(re.findall(r"(?m)^ok\b", text))
    notok = len(re.findall(r"(?m)^not ok\b", text))
    total = ok + notok
    return ParseResult(passed=ok, total=total, ran=total > 0,
                       detail=f"{ok}/{total} TAP assertions passed")


def parse_regex(results_dir, stdout, stderr, exit_code, spec) -> ParseResult:
    pattern = (spec or {}).get("regex") if isinstance(spec, dict) else getattr(spec, "regex", None)
    if not pattern:
        return ParseResult(detail="regex parser requires a 'regex' field in the verify spec")
    text = f"{stdout}\n{stderr}"
    m = None
    for m in re.finditer(pattern, text):
        pass
    if not m:
        return ParseResult(detail="regex did not match any test summary line")
    gd = m.groupdict()
    passed = int(gd["passed"]) if gd.get("passed") else 0
    if gd.get("total"):
        total = int(gd["total"])
    elif gd.get("failed"):
        total = passed + int(gd["failed"])
    else:
        total = passed
    return ParseResult(passed=passed, total=total, ran=total > 0,
                       detail=f"{passed}/{total} (regex)")


def parse_exit_code(results_dir, stdout, stderr, exit_code, spec) -> ParseResult:
    ok = exit_code == 0
    return ParseResult(passed=1 if ok else 0, total=1, ran=True,
                       detail="exit 0" if ok else f"exit {exit_code}")


_MAX_FAILED_CHECKS = 25
_MAX_CHECK_MESSAGE = 300


def _read_ctrf(results_dir: Path) -> tuple[int, int, list[str]] | None:
    """Read a CTRF report (``ctrf.json``) written by a Harbor verifier.

    Returns (passed, total, failed) where ``failed`` lists each failed check
    as ``name: message``. Returns None when there is no readable report.
    Skipped/pending checks count toward neither number.
    """
    for f in _glob_one(results_dir, "ctrf.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        results = data.get("results") if isinstance(data, dict) else None
        tests = results.get("tests") if isinstance(results, dict) else None
        if not isinstance(tests, list):
            continue
        passed = total = 0
        failed: list[str] = []
        for t in tests:
            if not isinstance(t, dict):
                continue
            status = str(t.get("status", "")).lower()
            if status in ("skipped", "pending"):
                continue
            total += 1
            if status == "passed":
                passed += 1
                continue
            name = str(t.get("name") or "?")
            # pytest ids: drop the file part ("test_outputs.py::Class::test_x").
            if "::" in name:
                name = name.split("::", 1)[1]
            msg = " ".join(str(t.get("message") or "").split())
            # pytest-json-ctrf puts a generic message ("The test failed in the
            # call phase ...") and the real assertion in `trace` as "E ..." lines.
            e_lines = [
                " ".join(line[1:].split())
                for line in str(t.get("trace") or "").splitlines()
                if line.startswith("E ") and line[1:].strip()
            ]
            if e_lines:
                msg = " | ".join(e_lines[:2])
            elif not msg:
                msg = " ".join(str(t.get("trace") or "").split())
            # Some graders repeat the check's label in the message; drop it once.
            label = name.split(":", 1)[1].strip() if ":" in name else ""
            if label and msg.startswith(label):
                msg = msg[len(label):].lstrip(" :")
            if len(msg) > _MAX_CHECK_MESSAGE:
                msg = msg[:_MAX_CHECK_MESSAGE] + "…"
            failed.append(f"{name}: {msg}" if msg else name)
        return passed, total, failed[:_MAX_FAILED_CHECKS]
    return None


def _with_checks(pr: ParseResult, results_dir: Path) -> ParseResult:
    """Add per-check results from ctrf.json to a reward-file result.

    The score is unchanged: it stays the reward the verifier wrote. Only the
    detail text and failed-check list are filled in, so a report can show
    which checks failed and why.
    """
    ctrf = _read_ctrf(results_dir)
    if ctrf is not None:
        passed, total, failed = ctrf
        pr.checks_detail = f"{passed}/{total} checks passed"
        pr.detail = f"{pr.detail} · {pr.checks_detail}"
        pr.failed_names = failed
    elif pr.score < 1.0:
        # No per-check report: the grader may have stopped before running any
        # checks (e.g. a missing dependency), which is not a model verdict.
        pr.checks_detail = "no per-check results written by the grader"
        pr.detail = f"{pr.detail} · {pr.checks_detail}"
    return pr


def parse_reward_file(results_dir, stdout, stderr, exit_code, spec) -> ParseResult:
    pr = _parse_reward_value(results_dir)
    return _with_checks(pr, results_dir) if pr.ran else pr


def _parse_reward_value(results_dir: Path) -> ParseResult:
    """Harbor / Terminal-Bench reward contract.

    A task's own verifier writes a reward to a file (Harbor's convention is
    ``/logs/verifier/reward.txt``; the generic docker runner maps that to
    ``$RESULTS_DIR``). The file holds either:

      * a single float in [0, 1]  — a graduated reward, mapped to score directly, or
      * two whitespace/slash-separated ints ``passed total`` (e.g. ``3 5`` or ``3/5``).

    Terminal-Bench 1.x tasks that only echo a binary reward (``0`` / ``1``) work
    too: ``1`` → 1/1, ``0`` → 0/1. Missing/empty/unparseable file → not ran
    (treated as a build/verifier failure by the caller), so a task that never
    produced a reward never silently scores as a pass.

    When the verifier also wrote a CTRF report (``ctrf.json``), the per-check
    counts and failure messages are added to the result's detail text by
    :func:`parse_reward_file`; the score still comes from the reward.
    """
    files = _glob_one(results_dir, "reward.txt", "reward", "*.reward", "reward.json")
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        # JSON reward: {"reward": 0.5} or {"passed": 3, "total": 5}
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                if "passed" in data and "total" in data:
                    total = int(data["total"])
                    passed = int(data["passed"])
                    return ParseResult(passed=passed, total=max(total, 0), ran=total > 0,
                                       detail=f"{passed}/{total} (reward.json)")
                rv = data.get("reward")
                if rv is not None:
                    r = max(0.0, min(1.0, float(rv)))
                    return ParseResult(passed=int(round(r * 1000)), total=1000, ran=True,
                                       detail=f"reward={r:.3f}")
        # "passed total" or "passed/total" integer pair.
        pair = re.match(r"^\s*(\d+)\s*[/\s]\s*(\d+)\s*$", text)
        if pair:
            passed, total = int(pair.group(1)), int(pair.group(2))
            return ParseResult(passed=passed, total=max(total, 0), ran=total > 0,
                               detail=f"{passed}/{total} (reward file)")
        # Single float/int reward in [0, 1].
        try:
            r = float(text.split()[0])
        except (ValueError, IndexError):
            continue
        r = max(0.0, min(1.0, r))
        # Preserve fractional rewards: represent as passed/1000 so `score` is exact.
        return ParseResult(passed=int(round(r * 1000)), total=1000, ran=True,
                           detail=f"reward={r:.3f}")
    return ParseResult(detail="no reward file produced by the task verifier "
                              "(expected reward.txt in $RESULTS_DIR)")


PARSERS = {
    "trx": parse_trx,
    "junit-xml": parse_junit_xml,
    "pytest-json": parse_pytest_json,
    "vitest-json": parse_vitest_json,
    "tap": parse_tap,
    "regex": parse_regex,
    "exit-code": parse_exit_code,
    "reward-file": parse_reward_file,
}


def parse_results(parser_name, results_dir, stdout, stderr, exit_code, spec=None) -> ParseResult:
    parser = PARSERS.get(parser_name)
    if parser is None:
        raise ValueError(
            f"Unknown verify parser '{parser_name}'. Available: {', '.join(sorted(PARSERS))}"
        )
    return parser(Path(results_dir), stdout or "", stderr or "", exit_code, spec)
