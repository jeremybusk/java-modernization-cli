"""Turn a failed build's raw output into a structured, agent-ready issue list.

javamod's OpenRewrite recipes do the deterministic, mechanical part of a
migration. What's left after a failed build is -- by definition -- the part
that needed a judgment call: a dependency with no automated migration path
(SpringFox), an API renamed with no drop-in replacement, or something nobody
here has seen before. This module does the deterministic part of *that*
too: parse the compiler/test output into distinct, located problems, and
attach a known remediation hint only where one is actually verified to
apply -- never a guess. An unmatched issue is reported with its facts only
(file, line, message) and null fix fields, so a human or an AI agent has
the real evidence to reason from instead of a fabricated suggestion.

The pattern table below is deliberately short: every entry was hit and
confirmed against a real build in this project's own history, not invented.
Extend it as new patterns are actually confirmed.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

KNOWN_PATTERNS: list[dict[str, Any]] = [
    {
        "name": "springfox-unmaintained",
        "signatures": ("springfox",),
        "category": "unresolved-dependency",
        "likely_cause": "springfox-swagger2 has no Spring Boot 3+/Jakarta support and is unmaintained.",
        "recommended_fix": "Replace the springfox-swagger2 dependency with springdoc-openapi "
                            "(e.g. org.springdoc:springdoc-openapi-starter-webmvc-ui) and rewrite "
                            "the Docket-based config to springdoc's OpenAPI bean/annotation API.",
        "confidence": "high",
    },
    {
        "name": "actuator-metrics-removed",
        "signatures": ("CounterService", "GaugeService"),
        "category": "removed-api",
        "likely_cause": "CounterService/GaugeService were removed from Spring Boot Actuator in 2.0, "
                         "replaced by Micrometer.",
        "recommended_fix": "Inject a Micrometer MeterRegistry and use registry.counter(name).increment() "
                            "/ registry.gauge(...) in place of CounterService/GaugeService calls.",
        "confidence": "high",
    },
    {
        "name": "spring-data-findone-removed",
        "signatures": ("findOne(",),
        "category": "renamed-api",
        "likely_cause": "Spring Data JPA renamed Repository.findOne(ID) to findById(ID) "
                         "(returning Optional<T>) in Spring Data 2.0.",
        "recommended_fix": "Replace .findOne(id) with .findById(id).orElseThrow(...), "
                            "handling the empty case explicitly.",
        "confidence": "high",
    },
    {
        "name": "pagerequest-two-arg-constructor-removed",
        "signatures": ("PageRequest cannot be applied",),
        "category": "changed-signature",
        "likely_cause": "PageRequest's public (int, int) constructor was removed; "
                         "Spring Data now requires the PageRequest.of(...) factory.",
        "recommended_fix": "Replace `new PageRequest(page, size)` with `PageRequest.of(page, size)` "
                            "(add a Sort argument if one was previously implied).",
        "confidence": "high",
    },
    {
        "name": "boot-recipe-stripped-unmanaged-dependency-version",
        "signatures": ("dependencies.dependency.version",),
        "category": "missing-dependency-version",
        "likely_cause": "An OpenRewrite Spring Boot upgrade recipe removed this dependency's explicit "
                         "<version>, assuming Spring Boot's own dependency-management BOM would supply "
                         "one -- but it doesn't for this dependency (verified: it's absent from the "
                         "published spring-boot-dependencies POM for the target version).",
        "recommended_fix": "Add back an explicit <version> for this dependency (check `git diff` on the "
                            "affected pom.xml for the version the recipe removed), or add a "
                            "<dependencyManagement> entry that pins one.",
        "confidence": "high",
    },
]

_MAVEN_ERROR_RE = re.compile(r"^\[ERROR\]\s+(?P<file>\S+\.java):\[(?P<line>\d+),\d+\]\s+(?P<message>.*)$")
_MAVEN_CONTINUATION_RE = re.compile(r"^\[ERROR\]\s{2,}(?P<text>.*)$")
_GRADLE_ERROR_RE = re.compile(r"^(?P<file>\S+\.java):(?P<line>\d+):\s*error:\s*(?P<message>.*)$")
_GRADLE_CONTINUATION_RE = re.compile(r"^\s{2,}(?P<text>\S.*)$")
_TEST_FAILURE_RE = re.compile(r"^\s{2}(?P<test>\S+\.\S+)\s+\u00bb\s+(?P<detail>.+)$")
# Surefire 3's end-of-module summary (one entry per failing test method):
#   [ERROR] Failures:
#   [ERROR]   ExchangeRatesClientTest.shouldRetrieveExchangeRates:26 expected: <null> but was: <USD>
#   [ERROR] Errors:
#   [ERROR]   FooTest.bar:12->helper:30 » IllegalState Failed to load ApplicationContext
# Rerun entries ("Run 1: ...") under a flaky test are skipped.
_SUREFIRE_BLOCK_RE = re.compile(r"^\[ERROR\] (Failures|Errors|Flakes):\s*$")
_SUREFIRE_ENTRY_RE = re.compile(
    r"^\[ERROR\]\s{2,}(?!Run \d+:)(?P<test>[^\s:\u00bb]+)(?::(?P<line>\d+))?(?:->\S*)?(?:\s+\u00bb)?(?:\s+(?P<detail>.*))?$"
)
# Gradle's per-test failure line, with the exception on the indented line after:
#   demo.BadTest > bad FAILED
#       java.lang.AssertionError at BadTest.java:12
_GRADLE_TEST_RE = re.compile(r"^(?P<cls>[\w.$]+) > (?P<method>.+) FAILED$")
_GRADLE_TEST_DETAIL_RE = re.compile(r"^\s+(?P<detail>\S.*?)(?: at \S+\.java:(?P<line>\d+))?$")
# Maven's "POM could not be read" failure (a reactor-level validation error, not
# a javac error) looks like:
#   [ERROR]   The project group:artifact:version (/path/to/pom.xml) has 1 error
#   [ERROR]     'dependencies.dependency.version' for group:artifact:jar is missing. @ line 58, column 15
_MAVEN_POM_PROJECT_RE = re.compile(r"^\[ERROR\]\s+The project \S+:\S+:\S+ \((?P<file>\S+\.xml)\) has \d+ error")
_MAVEN_POM_DETAIL_RE = re.compile(
    r"^\[ERROR\]\s+'(?P<field>[\w.]+)' for (?P<coord>[\w.\-]+:[\w.\-]+)(?::\w+)? is missing\.\s*@ line (?P<line>\d+)"
)


def _parse_compile_errors(output: str, error_re: re.Pattern, continuation_re: re.Pattern) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    current: list[str] | None = None
    for raw_line in output.splitlines():
        match = error_re.match(raw_line)
        if match:
            if current is not None:
                found.append((current[0], int(current[1]), current[2]))
            current = [match["file"], match["line"], match["message"]]
            continue
        if current is not None:
            continuation = continuation_re.match(raw_line)
            if continuation:
                current[2] += " " + continuation["text"]
                continue
            found.append((current[0], int(current[1]), current[2]))
            current = None
    if current is not None:
        found.append((current[0], int(current[1]), current[2]))
    return found


def _parse_test_failures(output: str) -> list[tuple[str, None, str]]:
    if "Tests in error:" not in output and "Tests in failure:" not in output:
        return []
    found = []
    in_block = False
    for raw_line in output.splitlines():
        if raw_line.strip() in {"Tests in error:", "Tests in failure:"}:
            in_block = True
            continue
        if in_block:
            match = _TEST_FAILURE_RE.match(raw_line)
            if match:
                found.append((match["test"], None, match["detail"]))
            elif raw_line.strip() == "" or raw_line.startswith("Tests run:"):
                in_block = False
    return found


def _parse_surefire_summary(output: str) -> list[tuple[str, int | None, str]]:
    found = []
    in_block = False
    for raw_line in output.splitlines():
        if _SUREFIRE_BLOCK_RE.match(raw_line):
            in_block = True
            continue
        if in_block:
            match = _SUREFIRE_ENTRY_RE.match(raw_line)
            if match:
                found.append((match["test"], int(match["line"]) if match["line"] else None,
                              (match["detail"] or "test failed").strip()))
            elif not raw_line.startswith("[ERROR]   "):
                in_block = False
    return found


def _parse_gradle_test_failures(output: str) -> list[tuple[str, int | None, str]]:
    found = []
    lines = output.splitlines()
    for index, raw_line in enumerate(lines):
        match = _GRADLE_TEST_RE.match(raw_line)
        if not match:
            continue
        test = f"{match['cls']}.{match['method'].removesuffix('()')}"
        detail = _GRADLE_TEST_DETAIL_RE.match(lines[index + 1]) if index + 1 < len(lines) else None
        found.append((test, int(detail["line"]) if detail and detail["line"] else None,
                      detail["detail"] if detail else "test failed"))
    return found


def _parse_maven_pom_validation_errors(output: str) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    current_file: str | None = None
    for raw_line in output.splitlines():
        project_match = _MAVEN_POM_PROJECT_RE.match(raw_line)
        if project_match:
            current_file = project_match["file"]
            continue
        if current_file is not None:
            detail_match = _MAVEN_POM_DETAIL_RE.match(raw_line)
            if detail_match:
                found.append((current_file, int(detail_match["line"]),
                              f"'{detail_match['field']}' for {detail_match['coord']} is missing"))
            elif raw_line.strip() in ("", "[ERROR]"):
                current_file = None
    return found


def _dedupe_by_location(raw: list[tuple[str, int | None, str]]) -> list[tuple[str, int | None, str]]:
    """Collapse duplicate (file, line) entries to their most informative message.

    Maven (and some Gradle setups) print the same diagnostics twice: once
    inline during compilation, again verbatim in the goal-failure summary.
    Without this, the two copies can pick up different amounts of
    continuation-line context and end up grouped as separate issues.
    """
    best: dict[tuple[str, int | None], str] = {}
    order: list[tuple[str, int | None]] = []
    for file, line, message in raw:
        key = (file, line)
        if key not in best:
            best[key] = message
            order.append(key)
            continue
        candidate_known = _match_known_pattern(message) is not None
        current_known = _match_known_pattern(best[key]) is not None
        if (candidate_known and not current_known) or (candidate_known == current_known and len(message) > len(best[key])):
            best[key] = message
    return [(file, line, best[(file, line)]) for file, line in order]


def _match_known_pattern(message: str) -> dict[str, Any] | None:
    for pattern in KNOWN_PATTERNS:
        if any(signature in message for signature in pattern["signatures"]):
            return pattern
    return None


def parse_build_failures(build_tool: str, output: str) -> list[dict[str, Any]]:
    """Group a failed build's raw output into distinct, located issues."""
    if build_tool == "maven":
        raw = _parse_compile_errors(output, _MAVEN_ERROR_RE, _MAVEN_CONTINUATION_RE)
        raw += _parse_maven_pom_validation_errors(output)
        tests = _parse_surefire_summary(output)
    else:
        raw = _parse_compile_errors(output, _GRADLE_ERROR_RE, _GRADLE_CONTINUATION_RE)
        tests = _parse_gradle_test_failures(output)
    tests += _parse_test_failures(output)
    test_names = {test for test, _line, _message in tests}
    raw = _dedupe_by_location(raw + tests)

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for file, line, message in raw:
        pattern = _match_known_pattern(message)
        key = (file, pattern["name"] if pattern else message[:80])
        if key not in grouped:
            grouped[key] = {
                "file": file,
                "lines": [],
                "category": pattern["category"] if pattern else ("test-failure" if file in test_names else "unknown"),
                "message": message.strip(),
                "likely_cause": pattern["likely_cause"] if pattern else None,
                "recommended_fix": pattern["recommended_fix"] if pattern else None,
                "confidence": pattern["confidence"] if pattern else "unverified",
            }
            order.append(key)
        if line is not None and line not in grouped[key]["lines"]:
            grouped[key]["lines"].append(line)
    return [grouped[key] for key in order]


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def to_yaml(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return "issues: []\n"
    lines = ["issues:"]
    for issue in issues:
        lines.append(f"  - file: {_yaml_scalar(issue['file'])}")
        lines.append(f"    lines: [{', '.join(str(n) for n in issue['lines'])}]")
        lines.append(f"    category: {_yaml_scalar(issue['category'])}")
        lines.append(f"    message: {_yaml_scalar(issue['message'])}")
        lines.append(f"    likely_cause: {_yaml_scalar(issue['likely_cause'])}")
        lines.append(f"    recommended_fix: {_yaml_scalar(issue['recommended_fix'])}")
        lines.append(f"    confidence: {_yaml_scalar(issue['confidence'])}")
    return "\n".join(lines) + "\n"


def write(issues: list[dict[str, Any]], destination: str) -> None:
    """Write the issue list as YAML to a file path, or to stdout if destination is '-'."""
    text = to_yaml(issues)
    if destination == "-":
        print(text, end="")
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def print_summary(issues: list[dict[str, Any]]) -> None:
    print(f"\n{len(issues)} residual issue(s) after migration (not fixed by recipes):")
    for issue in issues:
        location = issue["file"] + (":" + ",".join(map(str, issue["lines"])) if issue["lines"] else "")
        print(f"  [{issue['category']}] {location}")
        print(f"    {issue['message']}")
        if issue["recommended_fix"]:
            print(f"    -> {issue['recommended_fix']}")
