"""Compile (and optionally test) the migrated build, to catch anything the
recipes couldn't finish mechanically before it ever reaches the destination.
"""
from __future__ import annotations

import dataclasses
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from .discover import BuildRoot


# Enough for any real build log; triage needs the whole thing, since a large
# build's compile errors can scroll far above its final tail.
MAX_OUTPUT_CHARS = 5_000_000


@dataclasses.dataclass
class BuildResult:
    ok: bool
    command: list[str]
    output: str


def _executable(build: BuildRoot) -> str:
    wrapper = build.path / ("mvnw" if build.tool == "maven" else "gradlew")
    if wrapper.is_file():
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        return str(wrapper)
    return "mvn" if build.tool == "maven" else "gradle"


def _skip_file(build: BuildRoot, skip_tests: list[str]) -> Path:
    """Write the exclusion list in the build tool's own format, outside the repo.

    Maven: a ``surefire.excludesFile``, which is *appended* to the POM's own
    <excludes> (``-Dtest=!X`` would instead replace the POM's includes and
    excludes, quietly running tests the project deliberately leaves out).
    Gradle: an init script adding ``Test.exclude`` patterns, which works on
    every Gradle version (``excludeTestsMatching`` needs 5.0+).
    """
    def path_of(name: str) -> str:
        return name.replace(".", "/") if "." in name else "**/" + name
    if build.tool == "maven":
        text = "".join(f"{path_of(name)}.java\n" for name in skip_tests)
        suffix = ".txt"
    else:
        # Groovy single-quoted strings keep '$' literal for nested classes.
        def quote(value: str) -> str:
            return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
        patterns = ", ".join(f"{quote(path_of(name) + '.class')}, {quote(path_of(name) + '$*.class')}"
                             for name in skip_tests)
        text = f"allprojects {{ tasks.withType(Test) {{ exclude {patterns} }} }}\n"
        suffix = ".gradle"
    handle, name = tempfile.mkstemp(prefix="javamod-skip-tests-", suffix=suffix)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)
    return Path(name)


def validate(build: BuildRoot, *, run_tests: bool, skip_tests: list[str] = (), timeout: int = 1800) -> BuildResult:
    """Compile (and optionally test) the build, leaving out the *skip_tests* classes."""
    exe = _executable(build)
    skip_file = _skip_file(build, list(skip_tests)) if run_tests and skip_tests else None
    if build.tool == "maven":
        goal = "test" if run_tests else "test-compile"
        cmd = [exe, "--batch-mode", "--no-transfer-progress", goal]
        if skip_file:
            cmd.append(f"-Dsurefire.excludesFile={skip_file}")
    else:
        goal = "test" if run_tests else "testClasses"
        cmd = [exe, "--no-daemon", goal]
        if skip_file:
            cmd += ["--init-script", str(skip_file)]
    try:
        completed = subprocess.run(cmd, cwd=build.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, timeout=timeout, check=False)
        return BuildResult(ok=completed.returncode == 0, command=cmd, output=completed.stdout[-MAX_OUTPUT_CHARS:])
    except FileNotFoundError:
        return BuildResult(ok=False, command=cmd, output=f"{exe} is not installed or not on PATH")
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return BuildResult(ok=False, command=cmd, output=output[-MAX_OUTPUT_CHARS:] + "\n(timed out)")
    finally:
        if skip_file:
            skip_file.unlink(missing_ok=True)


_REACTOR_START = "[INFO] Reactor Summary"
# Maven's generic help footer, identical on every failure.
_MAVEN_BOILERPLATE = re.compile(
    r"^\[ERROR\]\s*(?:$|See dump files|-> \[Help|To see the full stack trace|Re-run Maven using|"
    r"For more information about the errors|\[Help \d+\] http|After correcting the problems|mvn <args> -rf)"
)
_GRADLE_KEEP = re.compile(r"FAILED|error:|^\* What went wrong|^> |^BUILD FAILED|^FAILURE:")


def condense(tool: str, output: str, max_lines: int = 120) -> str:
    """The lines of a failed build's output worth reading, without the log noise.

    Maven: every ``[ERROR]`` line plus the Reactor Summary (which module
    failed); framework/test logging -- often thousands of lines -- is dropped.
    Gradle: failed tasks/tests, compiler errors, the "What went wrong" block,
    and the indented detail under each. Falls back to the raw tail when
    nothing matches, so a failure is never reduced to nothing.
    """
    kept: list[str] = []
    in_reactor = False
    keep_indented = False
    for line in output.splitlines():
        if tool == "maven":
            if line.startswith(_REACTOR_START):
                in_reactor = True
            if in_reactor or (line.startswith("[ERROR]") and not _MAVEN_BOILERPLATE.match(line)):
                kept.append(line)
            if in_reactor and line.startswith("[INFO] BUILD"):
                in_reactor = False
        else:
            if _GRADLE_KEEP.search(line):
                kept.append(line)
                keep_indented = True
            elif keep_indented and line.startswith((" ", "\t")) and line.strip():
                kept.append(line)
            else:
                keep_indented = False
    if not kept:
        kept = output.splitlines()
    return "\n".join(kept[-max_lines:])
