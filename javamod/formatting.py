"""Reconcile OpenRewrite's output with a project's own enforced formatter.

Some projects bind a formatter's "check" goal into their build (most
commonly Spring's own `spring-javaformat` or `Spotless`), so any tool that
rewrites source -- OpenRewrite included -- produces a tree that compiles and
behaves correctly but still fails validation on formatting alone. Detect the
formatter the project itself declared and run its "apply" goal/task before
validating, the same way a developer would locally.

Goal/task names verified against each project's own README, not assumed:
spring-io/spring-javaformat and diffplug/spotless.
"""
from __future__ import annotations

import subprocess

from .buildcheck import _executable
from .discover import BuildRoot
from .errors import ModError

# (plugin marker to search for, goal/task to run), checked in this order.
_MAVEN_FORMATTERS = (
    ("spring-javaformat-maven-plugin", "spring-javaformat:apply"),
    ("spotless-maven-plugin", "spotless:apply"),
)
_GRADLE_FORMATTERS = (
    ("io.spring.javaformat", "format"),
    ("com.diffplug.spotless", "spotlessApply"),
)


def detect_goal(build: BuildRoot) -> str | None:
    """Return the apply goal/task for whichever formatter the project declares, if any."""
    if build.tool == "maven":
        text = (build.path / "pom.xml").read_text(encoding="utf-8", errors="ignore")
        candidates = _MAVEN_FORMATTERS
    else:
        text = "".join(
            (build.path / name).read_text(encoding="utf-8", errors="ignore")
            for name in ("build.gradle", "build.gradle.kts") if (build.path / name).is_file()
        )
        candidates = _GRADLE_FORMATTERS
    for marker, goal in candidates:
        if marker in text:
            return goal
    return None


def reconcile(build: BuildRoot, *, log=print, timeout: int = 600) -> bool:
    """Run the detected formatter's apply goal, if any. Returns whether one ran."""
    goal = detect_goal(build)
    if goal is None:
        return False
    exe = _executable(build)
    cmd = [exe, "--batch-mode", goal] if build.tool == "maven" else [exe, "--no-daemon", goal]
    log(f"reconciling formatting with the project's own plugin ({goal})")
    try:
        completed = subprocess.run(cmd, cwd=build.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise ModError(f"command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ModError(f"timed out reconciling formatting: {' '.join(cmd)}") from exc
    if completed.returncode != 0:
        raise ModError(f"formatting reconciliation failed ({completed.returncode}): {' '.join(cmd)}\n"
                        f"{completed.stdout[-4000:]}")
    return True
