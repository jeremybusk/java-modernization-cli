"""Optional coding-agent stage: hand the working clone to an agent CLI you
already use (Claude Code, Codex, GitHub Copilot CLI) after the recipes run,
optionally with Agent Skills installed into the clone for it to use.

This is a different shape from ``--engine hybrid``/``ai`` (``ai.py``), which
call the Anthropic API directly with a fixed prompt. An agent CLI can explore
the repo, run the build itself, iterate, and pull in skills -- portable
``SKILL.md`` packages such as java.evolved's ``modern-java`` -- so it covers
the long tail that recipes don't, using whatever subscription/login the CLI
already has instead of an API key.

javamod stays in charge of everything around it: the agent only edits files.
The skills it was given are kept out of the commit, anything it commits on its
own is folded back into javamod's single commit, and javamod's own build
check -- not the agent's say-so -- decides whether the result can be pushed.
"""
from __future__ import annotations

import dataclasses
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from . import gitrepo
from .discover import BuildRoot
from .errors import ModError

# Each agent CLI run non-interactively, with edits allowed and shell access
# limited (where the CLI supports it) to the build tool and read-only git.
# ``skills_dir`` is where that CLI discovers project-local Agent Skills.
CLAUDE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Skill", "Bash(mvn:*)", "Bash(./mvnw:*)",
                "Bash(gradle:*)", "Bash(./gradlew:*)", "Bash(git status:*)", "Bash(git diff:*)")


@dataclasses.dataclass(frozen=True)
class AgentCli:
    binary: str
    skills_dir: str
    args: tuple[str, ...]
    prompt_flag: str | None  # None: the prompt is the last positional argument


AGENTS = {
    "claude": AgentCli("claude", ".claude/skills",
                       ("--permission-mode", "acceptEdits", "--allowedTools", *CLAUDE_TOOLS), "-p"),
    # workspace-write keeps codex inside the clone; Maven/Gradle still need the network.
    "codex": AgentCli("codex", ".agents/skills",
                      ("exec", "--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"),
                      None),
    "copilot": AgentCli("copilot", ".github/skills", ("--allow-all-tools",), "-p"),
    # Microsoft's GitHub Copilot modernization agent. It's Copilot-CLI-only and
    # its license forbids repackaging, so install it yourself first:
    #   copilot plugin marketplace add microsoft/modernize-java
    #   copilot plugin install modernize-java@modernize-java
    "copilot-modernize-java": AgentCli("copilot", ".github/skills",
                                       ("--allow-all-tools", "--agent", "modernize-java:modernize-java"), "-p"),
}

# Short names for skills known to fit this job, pinned to a reviewed commit so
# a run is reproducible and an upstream change can't silently alter it.
# Anything else: a local directory containing SKILL.md, or 'git-url#subdir'.
BUILTIN_SKILLS = {
    # Version-aware "write idiomatic Java N" guidance (MIT).
    "modern-java": ("https://github.com/brunoborges/javaevolved.git", "19d2cd173ff929777d1ff85d036f77d7c8c2d6a4",
                    "agent-plugins/modern-java-development/skills/modern-java"),
    # What breaks between LTS versions and how to fix it (Apache-2.0).
    "java-version-upgrade": ("https://github.com/G10xy/java-version-upgrade-skill.git",
                             "7c043a00d46bcf2997fd7cc4da6447900e562f00", ""),
}

NAME_RE = re.compile(r"^name:\s*['\"]?([\w.-]+)", re.MULTILINE)


def cli_for(name: str) -> AgentCli:
    agent = AGENTS[name]
    if shutil.which(agent.binary) is None:
        raise ModError(f"--agent {name} needs the '{agent.binary}' CLI on PATH (and logged in)")
    return agent


def _fetch(url: str, commit: str | None, dest: Path) -> None:
    dest.mkdir(parents=True)
    if commit:
        gitrepo.run(["git", "init", "-q"], cwd=dest)
        gitrepo.run(["git", "fetch", "-q", "--depth", "1", url, commit], cwd=dest)
        gitrepo.run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest)
    else:
        gitrepo.run(["git", "clone", "-q", "--depth", "1", url, "."], cwd=dest)


def resolve_skill(spec: str, cache: Path) -> Path:
    """Return a local directory holding SKILL.md for a builtin name, path, or 'git-url#subdir'."""
    if spec in BUILTIN_SKILLS:
        url, commit, subdir = BUILTIN_SKILLS[spec]
    elif Path(spec).expanduser().is_dir():
        url, commit, subdir = None, None, ""
    elif gitrepo.is_remote(spec.split("#", 1)[0]):
        url, _, subdir = spec.partition("#")
        commit = None
    else:
        raise ModError(f"unknown --agent-skill '{spec}': use one of {sorted(BUILTIN_SKILLS)}, "
                       "a local directory containing SKILL.md, or 'git-url#path/to/skill'")
    if url is None:
        path = Path(spec).expanduser().resolve()
    else:
        checkout = cache / re.sub(r"\W+", "-", spec).strip("-")
        if not checkout.exists():
            _fetch(url, commit, checkout)
        path = checkout / subdir
    if not (path / "SKILL.md").is_file():
        raise ModError(f"--agent-skill '{spec}': no SKILL.md at {path}")
    return path


def skill_name(path: Path) -> str:
    match = NAME_RE.search((path / "SKILL.md").read_text(encoding="utf-8", errors="ignore"))
    return match.group(1) if match else path.name


def install_skills(repo: Path, agent: AgentCli, specs: list[str], cache: Path) -> list[str]:
    """Copy each skill into the clone where *agent* finds it, excluded from the commit."""
    names = []
    for spec in specs:
        source = resolve_skill(spec, cache)
        name = skill_name(source)
        target = repo / agent.skills_dir / name
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
        gitrepo.exclude(repo, [f"/{agent.skills_dir}/{name}/"])
        names.append(name)
    return names


TEST_RULES = (
    "Never delete, disable (@Disabled, @Ignore, assumptions, build-tool skips), or weaken a test or its "
    "assertions, and never change an expected value to match new behavior unless the upgrade itself "
    "legitimately changed that behavior. If a failure comes from outside the code -- a live external "
    "service, network access, missing credentials, the local environment -- make no change for it and "
    "say so, with your evidence, in your final reply."
)
RULES = (
    "Rules: edit files only. Do NOT run git commit, git push, or change branches -- the calling tool "
    "commits and validates the result itself. Don't create notes, plans, or summary files in the repo; "
    "put your summary in your final reply. " + TEST_RULES
)


def _skipped_note(skip_tests: list[str]) -> list[str]:
    if not skip_tests:
        return []
    return [f"These test classes are excluded from validation by the user as known failures unrelated to the "
            f"upgrade; leave them and their failures alone: {', '.join(skip_tests)}."]


def build_prompt(build: BuildRoot, repo: Path, applied_recipes: list[str], *, target_java: int, boot: str | None,
                 skills: list[str], run_tests: bool, skip_tests: list[str] = (), failure: str | None = None) -> str:
    root = build.path.relative_to(repo).as_posix()
    goal = "test" if run_tests else ("test-compile" if build.tool == "maven" else "testClasses")
    recipes = "\n".join(f"  - {name}" for name in applied_recipes) or "  (none)"
    lines = [
        f"This repository is being modernized to Java {target_java}"
        + (f" and Spring Boot {boot}" if boot else "") + ".",
        f"It is a {build.tool} build rooted at '{root}' (detected current Java: {build.current_java or 'unknown'}).",
        f"These OpenRewrite recipes have already been applied:\n{recipes}",
        *(["Use these installed skills where they apply: " + ", ".join(skills) + "."] if skills else []),
        *_skipped_note(list(skip_tests)),
        *([f"The calling tool's build check currently fails; the relevant part of its output:\n\n{failure}"]
          if failure else []),
        "",
        "Finish the job:",
        f"1. Run the build ({build.tool} {goal}) and fix whatever fails, preserving behavior and public API.",
        f"2. Then fix remaining issues the recipes don't cover for Java {target_java}: removed or "
        "deprecated-for-removal APIs, build-plugin versions too old for the target JDK, and similar.",
        "3. Keep changes focused on the upgrade; don't reformat untouched code or restructure the project.",
        "",
        RULES,
    ]
    return "\n".join(lines)


def retry_prompt(build: BuildRoot, repo: Path, *, failure: str, attempt: int, skills: list[str],
                 skip_tests: list[str] = ()) -> str:
    root = build.path.relative_to(repo).as_posix()
    return "\n".join([
        f"Follow-up pass {attempt}. You (or a previous pass) already worked on upgrading this {build.tool} build "
        f"rooted at '{root}'. The calling tool then ran its own build check, which still fails. The relevant "
        f"part of its output:",
        "",
        failure,
        "",
        *(["Use these installed skills where they apply: " + ", ".join(skills) + "."] if skills else []),
        *_skipped_note(list(skip_tests)),
        "Find the root cause of each failure above and fix it, preserving behavior and public API. "
        "Rerun the build to confirm.",
        "",
        RULES,
    ])


def install(repo: Path, agent_name: str, specs: list[str], cache: Path, log=print) -> list[str]:
    installed = install_skills(repo, cli_for(agent_name), specs, cache)
    if installed:
        log(f"agent: installed skill(s) {', '.join(installed)} into {AGENTS[agent_name].skills_dir}/")
    return installed


def run(repo: Path, prompt: str, *, agent_name: str, model: str | None, extra_args: list[str], timeout: int,
        log_path: Path, log=print) -> bool:
    """Run one agent pass on *repo*; True if the CLI exited cleanly. Appends to the transcript."""
    agent = cli_for(agent_name)
    cmd = [agent.binary, *agent.args, *(["--model", model] if model else []), *extra_args]
    cmd += [agent.prompt_flag, prompt] if agent.prompt_flag else [prompt]
    log(f"agent: running {agent_name} (transcript: {log_path})")
    head = gitrepo.capture(["git", "rev-parse", "HEAD"], repo)
    try:
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write("=== $ " + shlex.join(cmd[:-1] + ["<prompt>"]) + "\n\n" + prompt + "\n\n---\n")
            stream.flush()
            completed = subprocess.run(cmd, cwd=repo, stdin=subprocess.DEVNULL, stdout=stream,
                                       stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False)
            stream.write("\n")
        ok = completed.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"agent: timed out after {timeout}s; keeping whatever it changed")
        ok = False
    # Fold any commits the agent made anyway back into the working tree, so
    # the run still ends in one javamod commit on top of the source ref.
    if gitrepo.capture(["git", "rev-parse", "HEAD"], repo) != head:
        log("agent: it committed on its own; folding those commits into javamod's commit")
        gitrepo.run(["git", "reset", "-q", "--soft", head], cwd=repo)
    if not ok:
        log(f"agent: {agent_name} did not finish cleanly; see {log_path}")
    return ok
