"""Optional Claude-assisted pass, used two ways depending on ``--engine``:

* ``hybrid`` (OpenRewrite + AI): if the build still fails after the recipes
  run, feed the compiler output and the implicated files to the model and
  apply the full-file rewrites it returns, then rebuild. This is where an
  LLM earns its keep -- finishing the long tail of judgment calls recipes
  don't cover, with a compiler checking its work every iteration.
* ``ai`` (AI only, no OpenRewrite): ask the model to modernize each source
  file toward the target Java version directly. No recipes, no safety net
  beyond the same build-and-retry loop, so it's best for small repos or
  builds OpenRewrite can't touch -- not a wholesale replacement for it.

Both need ``pip install anthropic`` and ``ANTHROPIC_API_KEY``; neither is
imported unless the engine is actually used.
"""
from __future__ import annotations

import json
import re
from itertools import islice
from pathlib import Path

from . import buildcheck, triage
from .discover import BuildRoot, _module_dirs, java_sources
from .errors import ModError

DEFAULT_MODEL = "claude-sonnet-5"
MAX_FILE_BYTES = 60_000


def _client():
    try:
        import anthropic
    except ImportError as exc:
        raise ModError("the AI engine needs the 'anthropic' package: pip install anthropic") from exc
    try:
        return anthropic.Anthropic()
    except anthropic.AnthropicError as exc:
        raise ModError(f"could not create an Anthropic client (check ANTHROPIC_API_KEY): {exc}") from exc


def _ask_for_files(client, model: str, system: str, user: str) -> dict[str, str]:
    response = client.messages.create(
        model=model, max_tokens=8192, system=system, messages=[{"role": "user", "content": user}],
    )
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise ModError("the model response was truncated; no files were changed")
    text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text.strip())
    try:
        items = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModError(f"the model did not return valid JSON: {exc}") from exc
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ModError("the model must return a JSON array of file objects")
    files = {}
    for item in items:
        if not item.get("path"):
            continue
        if not isinstance(item["path"], str) or not isinstance(item.get("content"), str):
            raise ModError("each model file needs a string path and string content")
        files[item["path"]] = item["content"]
    return files


def implicated_files(build: BuildRoot, output: str, limit: int = 12) -> list[str]:
    """Source files the compiler blamed, relative to the build root.

    Reuses triage's parsers, which know Maven's ``[ERROR] /abs/File.java:[l,c]``
    and Gradle's ``/abs/File.java:l: error:`` shapes; failing *tests* are
    reported by class.method, not a file, so they're skipped here.
    """
    root = build.path.resolve()
    files: list[str] = []
    for issue in triage.parse_build_failures(build.tool, output):
        path = Path(issue["file"])
        path = (path if path.is_absolute() else root / path).resolve()
        if path.suffix == ".java" and path.is_relative_to(root) and path.is_file():
            rel = path.relative_to(root).as_posix()
            if rel not in files:
                files.append(rel)
    return files[:limit]


def fix_build(build: BuildRoot, result: buildcheck.BuildResult, *, model: str, max_iterations: int,
              run_tests: bool, skip_tests: list[str] = (), log=print) -> buildcheck.BuildResult:
    """Iteratively ask the model to fix a failing build, rebuilding each time."""
    client = None
    for attempt in range(1, max_iterations + 1):
        implicated = implicated_files(build, result.output)
        if not implicated:
            log("ai: build failed with no .java file references to act on; stopping")
            return result
        files = {}
        for rel in implicated:
            path = build.path / rel
            if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
                files[rel] = path.read_text(encoding="utf-8", errors="ignore")
        if not files:
            return result
        if client is None:
            client = _client()
        log(f"ai: build fix attempt {attempt}/{max_iterations} on {len(files)} file(s)")
        user = (
            "This Java build is failing after an automated modernization pass. "
            "Fix only what's needed to make it compile and pass, preserving behavior.\n\n"
            f"Build output (tail):\n{result.output[-6000:]}\n\n"
            "Files (path -> content):\n" + json.dumps(files, indent=2)
        )
        system = (
            "You are fixing a Java build. Respond with ONLY a JSON array of "
            '{"path": "...", "content": "..."} objects, one per file you changed, '
            "full file content each time, no markdown fences, no commentary."
        )
        fixes = _ask_for_files(client, model, system, user)
        if not fixes:
            return result
        for rel, content in fixes.items():
            if rel not in files:
                log(f"ai: ignoring a rewrite of a file not supplied to the model: {rel}")
                continue
            target = (build.path / rel).resolve()
            if not target.is_relative_to(build.path.resolve()):
                log(f"ai: ignoring a rewrite outside the build: {rel}")
                continue
            target.write_text(content, encoding="utf-8")
        result = buildcheck.validate(build, run_tests=run_tests, skip_tests=skip_tests)
        if result.ok:
            return result
    return result


SOURCE_DIRS = ("src/main/java", "src/test/java")


def modernize_tree(build: BuildRoot, *, target_java: int, model: str, max_files: int, log=print) -> list[str]:
    """Ask the model to modernize each source file toward *target_java*. Returns changed paths."""
    client = _client()
    files = islice((p for directory in _module_dirs(build.path, build.tool)
                    for sub in SOURCE_DIRS for p in java_sources(directory / sub)
                    if p.stat().st_size <= MAX_FILE_BYTES), max_files)
    system = (
        f"You modernize Java source to idiomatic Java {target_java}: var where it reads better, "
        "text blocks, switch expressions, pattern matching, records where appropriate, and removal of "
        "deprecated-for-removal APIs with their direct replacement. Preserve behavior and public API "
        "exactly. Respond with ONLY the complete rewritten file content, no markdown fences, no "
        "commentary. If nothing should change, return the file unchanged."
    )
    changed: list[str] = []
    for path in files:
        original = path.read_text(encoding="utf-8", errors="ignore")
        response = client.messages.create(
            model=model, max_tokens=8192, system=system,
            messages=[{"role": "user", "content": original}],
        )
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise ModError(f"the model response for {path.relative_to(build.path)} was truncated; file unchanged")
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        text = re.sub(r"^```[a-z]*\n|\n```$", "", text.strip())
        if text and text != original:
            path.write_text(text, encoding="utf-8")
            changed.append(str(path.relative_to(build.path)))
            log(f"ai: modernized {path.relative_to(build.path)}")
    return changed
