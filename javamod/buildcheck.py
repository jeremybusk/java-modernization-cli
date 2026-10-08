"""Compile (and optionally test) the migrated build, to catch anything the
recipes couldn't finish mechanically before it ever reaches the destination.
"""
from __future__ import annotations

import dataclasses
import stat
import subprocess
from pathlib import Path

from .discover import BuildRoot


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


def validate(build: BuildRoot, *, run_tests: bool, timeout: int = 1800) -> BuildResult:
    exe = _executable(build)
    if build.tool == "maven":
        goal = "test" if run_tests else "test-compile"
        cmd = [exe, "--batch-mode", "--no-transfer-progress", goal]
    else:
        goal = "test" if run_tests else "testClasses"
        cmd = [exe, "--no-daemon", goal]
    try:
        completed = subprocess.run(cmd, cwd=build.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, timeout=timeout, check=False)
        return BuildResult(ok=completed.returncode == 0, command=cmd, output=completed.stdout[-20_000:])
    except FileNotFoundError:
        return BuildResult(ok=False, command=cmd, output=f"{exe} is not installed or not on PATH")
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        return BuildResult(ok=False, command=cmd, output=output[-20_000:] + "\n(timed out)")
