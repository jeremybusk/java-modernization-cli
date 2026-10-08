"""The ``javamod`` command line: ``javamod migrate`` and ``javamod doctor``.

Design goal: one command, sensible defaults, repeatable across many repos
with no config file. Every tunable below also reads a ``JAVAMOD_*``
environment variable, so a team can export its target Java/Boot
version/profile/recipe-source once and then run the bare command the same
way against every repository.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
import tempfile
import urllib.parse
from pathlib import Path

from . import agent, ai, buildcheck, discover, formatting, gitrepo, openrewrite, recipes, triage
from .envutil import env_default
from .errors import ModError
from .report import RunReport


def _host_of(location: str) -> str:
    if "://" in location:
        return urllib.parse.urlsplit(location).hostname or ""
    if "@" in location and ":" in location:
        return location.split("@", 1)[1].split(":", 1)[0]
    return ""


def _resolve_token_env(args: argparse.Namespace) -> str | None:
    if args.token_env:
        return args.token_env
    if args.provider == "github":
        return "GH_TOKEN"
    if args.provider == "gitlab":
        return "GITLAB_TOKEN"
    for location in (args.dest, args.source):
        host = _host_of(location or "")
        if "github" in host:
            return "GH_TOKEN"
        if "gitlab" in host:
            return "GITLAB_TOKEN"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="javamod", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate", help="modernize a source repo:ref and publish it to a destination repo:branch")
    migrate.add_argument("--source", default=env_default("JAVAMOD_SOURCE", None),
                          required=env_default("JAVAMOD_SOURCE", None) is None,
                          help="git URL or local path; optionally 'location#ref'")
    migrate.add_argument("--source-ref", default=env_default("JAVAMOD_SOURCE_REF", None),
                          help="branch, tag, or commit; overrides a '#ref' suffix on --source")
    migrate.add_argument("--dest", default=env_default("JAVAMOD_DEST", None),
                          help="existing git URL or local repo to push the result to (required unless --local-only)")
    migrate.add_argument("--dest-branch", default=env_default("JAVAMOD_DEST_BRANCH", None),
                          required=env_default("JAVAMOD_DEST_BRANCH", None) is None,
                          help="branch to create/update, locally and (unless --local-only) at --dest")
    migrate.add_argument("--build-root", default=None, help="path within the source to the Maven/Gradle build, for monorepos")

    migrate.add_argument("--java", type=int, default=env_default("JAVAMOD_JAVA", 21, int),
                          choices=recipes.TARGET_JAVA_VERSIONS, help="target Java version (default: %(default)s)")
    migrate.add_argument("--boot", default=env_default("JAVAMOD_BOOT", None), help="target Spring Boot, e.g. 3.5 or 4.0")
    migrate.add_argument("--profile", default=env_default("JAVAMOD_PROFILE", "standard"),
                          choices=sorted(recipes.PROFILE_DEFAULTS), help="how much to change beyond the Java upgrade itself")
    migrate.add_argument("--dependency-strategy", default=env_default("JAVAMOD_DEPENDENCY_STRATEGY", "patch"),
                          choices=("patch", "latest"), help="only used when the profile upgrades dependencies")
    migrate.add_argument("--recipe", action="append", default=[], help="extra OpenRewrite recipe name, repeatable")

    migrate.add_argument("--recipe-source", default=env_default("JAVAMOD_RECIPE_SOURCE", "maven-central"),
                          choices=("maven-central", "source", "codegenome"),
                          help="where recipe jars come from (default: %(default)s)")
    migrate.add_argument("--allow-codegenome", action="store_true",
                          default=env_default("JAVAMOD_ALLOW_CODEGENOME", False),
                          help="required in addition to --recipe-source codegenome")
    migrate.add_argument("--codegenome-username-env", default="CODE_GENOME_USERNAME")
    migrate.add_argument("--codegenome-token-env", default="CODE_GENOME_TOKEN")

    migrate.add_argument("--engine", default=env_default("JAVAMOD_ENGINE", "openrewrite"),
                          choices=("openrewrite", "hybrid", "ai"),
                          help="openrewrite (default): deterministic recipes only. hybrid: recipes, then an AI "
                               "pass only if the build still fails. ai: no recipes, AI modernizes files directly.")
    migrate.add_argument("--ai-model", default=env_default("JAVAMOD_AI_MODEL", ai.DEFAULT_MODEL))
    migrate.add_argument("--ai-max-iterations", type=int, default=env_default("JAVAMOD_AI_MAX_ITERATIONS", 3, int))
    migrate.add_argument("--ai-max-files", type=int, default=env_default("JAVAMOD_AI_MAX_FILES", 40, int))

    migrate.add_argument("--agent", default=env_default("JAVAMOD_AGENT", None), choices=sorted(agent.AGENTS),
                          help="after the recipes, hand the clone to this coding-agent CLI to finish the upgrade "
                               "(uses the CLI's own login; off by default)")
    migrate.add_argument("--agent-skill", action="append",
                          default=[s for s in env_default("JAVAMOD_AGENT_SKILLS", "").split(",") if s],
                          help=f"Agent Skill to install for --agent, repeatable: one of {sorted(agent.BUILTIN_SKILLS)}, "
                               "a local directory containing SKILL.md, or 'git-url#path/to/skill'")
    migrate.add_argument("--agent-model", default=env_default("JAVAMOD_AGENT_MODEL", None),
                          help="model passed to the agent CLI's --model (default: the CLI's own default)")
    migrate.add_argument("--agent-arg", action="append", default=[],
                          help="extra argument passed through to the agent CLI, repeatable (use --agent-arg=--flag)")
    migrate.add_argument("--agent-timeout", type=int, default=env_default("JAVAMOD_AGENT_TIMEOUT", 3600, int),
                          help="seconds before each agent pass is stopped (default: %(default)s)")
    migrate.add_argument("--agent-on", default=env_default("JAVAMOD_AGENT_ON", "always"), choices=("always", "failure"),
                          help="always (default): run the agent after the recipes. failure: only if javamod's build "
                               "check fails after them, saving an agent pass when the recipes were enough")
    migrate.add_argument("--agent-retries", type=int, default=env_default("JAVAMOD_AGENT_RETRIES", 0, int),
                          help="if javamod's own build check still fails after the agent, give the agent up to this "
                               "many more passes with that failure output (default: %(default)s)")

    migrate.add_argument("--skip-build", action="store_true", help="skip compiling/testing the result")
    migrate.add_argument("--skip-format", action="store_true",
                          help="don't run the project's own formatter (spring-javaformat/Spotless) after migrating, "
                               "even if one is detected")
    migrate.add_argument("--skip-tests", action="store_true", help="compile only; don't run the test suite")
    migrate.add_argument("--skip-test", action="append", metavar="CLASS",
                          default=[t for t in env_default("JAVAMOD_SKIP_TESTS", "").split(",") if t],
                          help="exclude this test class (simple or fully qualified name) from the build check, "
                               "repeatable; for tests known to fail for reasons outside the migration, e.g. a "
                               "live external service. Recorded in the report and commit message.")
    migrate.add_argument("--shallow", action="store_true", help="shallow-clone a remote source (loses history)")
    migrate.add_argument("--allow-dirty", action="store_true", help="allow a local source with uncommitted changes")

    migrate.add_argument("--execute", action="store_true", help="push the result; without this, javamod only plans and commits locally")
    migrate.add_argument("--local-only", action="store_true", help="commit locally and never push, even with --execute")
    migrate.add_argument("--init-dest", action="store_true", help="git init --bare a local --dest path if it doesn't exist")
    migrate.add_argument("--force-push", action="store_true", help="push even if the build/tests failed, and overwrite a diverged destination branch")
    migrate.add_argument("--provider", choices=("github", "gitlab"), default=env_default("JAVAMOD_PROVIDER", None),
                          help="selects the HTTPS push token env var (GH_TOKEN / GITLAB_TOKEN); auto-detected from the URL otherwise")
    migrate.add_argument("--token-env", default=env_default("JAVAMOD_TOKEN_ENV", None), help="explicit env var holding the HTTPS push token")

    migrate.add_argument("--workdir", type=Path, default=None, help="use this directory instead of a temp dir; kept after the run")
    migrate.add_argument("--keep", action="store_true", help="keep the temp workdir even on success")
    migrate.add_argument("--report", default=None, help="write the JSON run report here, or '-' for stdout (CI-friendly)")
    migrate.add_argument("--issues", default=None,
                          help="if the build still fails, write the residual-issue triage YAML here, or '-' for "
                               "stdout; default: <workdir>/remaining-issues.yaml")
    migrate.add_argument("--diff-stat-lines", type=int, default=env_default("JAVAMOD_DIFF_STAT_LINES", 25, int),
                          help="max changed-file lines in the printed summary, 0 for all (the JSON report always "
                               "has the full list; default: %(default)s)")
    migrate.add_argument("--quiet", action="store_true", help="suppress the human-readable summary (pairs with --report -)")
    migrate.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation before pushing")
    migrate.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("doctor", help="check that git/java/maven/gradle (and, if needed, the anthropic package) are available")
    return parser


def cmd_doctor(_args: argparse.Namespace) -> int:
    checks = [("git", "git"), ("java", "java"), ("javac", "javac"), ("mvn", "mvn"), ("gradle", "gradle"), ("gh", "gh")]
    found = {name: shutil.which(binary) for name, binary in checks}
    for name, path in found.items():
        print(f"{'OK     ' if path else 'MISSING'} {name}" + (f"  ({path})" if path else ""))
    try:
        import anthropic  # noqa: F401
        print("OK      anthropic (python package, for --engine ai/hybrid)")
        ai_ok = True
    except ImportError:
        print("MISSING anthropic (python package; only needed for --engine ai/hybrid): pip install anthropic")
        ai_ok = True  # not required for the default engine
    for name in ("claude", "codex", "copilot"):
        path = shutil.which(name)
        print(f"{'OK     ' if path else 'MISSING'} {name} (optional, for --agent)" + (f"  ({path})" if path else ""))
    ok = bool(found["git"] and found["java"] and found["javac"] and (found["mvn"] or found["gradle"])) and ai_ok
    if not ok:
        print("\nOn Ubuntu/Debian:\n  sudo apt-get update && sudo apt-get install -y git openjdk-21-jdk maven gradle")
        print("\n(or open this repo in its devcontainer, which ships all of the above)")
    return 0 if ok else 1


def _agent_pass(args: argparse.Namespace, repo: Path, prompt: str, log_path: Path, log) -> bool:
    return agent.run(repo, prompt, agent_name=args.agent, model=args.agent_model, extra_args=args.agent_arg,
                     timeout=args.agent_timeout, log_path=log_path, log=log)


def cmd_migrate(args: argparse.Namespace) -> int:
    source, inline_ref = gitrepo.split_ref(args.source)
    ref = args.source_ref or inline_ref
    log = (lambda msg: print(f"[javamod] {msg}", file=sys.stderr)) if args.verbose else (lambda _msg: None)
    human_stream = sys.stderr if args.report == "-" or args.issues == "-" else sys.stdout

    persistent = args.workdir is not None
    workdir = (args.workdir or Path(tempfile.mkdtemp(prefix="javamod-"))).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    push_token_env = _resolve_token_env(args)
    success = False
    report = None
    try:
        log(f"cloning {source}" + (f"#{ref}" if ref else ""))
        src_path = gitrepo.clone_source(
            source, ref, workdir, allow_dirty=args.allow_dirty, shallow=args.shallow,
            token_env=push_token_env if gitrepo.is_remote(source) else None,
        )

        base_rev = gitrepo.capture(["git", "rev-parse", "HEAD"], src_path)
        build = discover.find_build_root(src_path, args.build_root)
        log(f"{build.tool} build at {build.path.relative_to(src_path)}"
            f" (current Java: {build.current_java or 'unknown'}, features: {sorted(build.features) or 'none detected'})")
        gitrepo.ignore_build_outputs(src_path, build.path, build.tool)
        boot = args.boot if build.spring_boot else None
        if args.boot and not boot and not args.quiet:
            print(f"warning: --boot {args.boot} ignored: no Spring Boot detected in this build", file=sys.stderr)

        branch = args.dest_branch
        gitrepo.ensure_branch(src_path, branch)

        plan = recipes.build_plan(
            build, target_java=args.java, boot=boot, profile=args.profile,
            dependency_strategy=args.dependency_strategy, extra_recipes=tuple(args.recipe),
        )
        if args.engine in ("openrewrite", "hybrid"):
            openrewrite.run(
                build, plan, recipe_source=args.recipe_source, allow_codegenome=args.allow_codegenome,
                codegenome_username_env=args.codegenome_username_env,
                codegenome_token_env=args.codegenome_token_env, log=log,
            )
        if args.engine == "ai":
            ai.modernize_tree(build, target_java=args.java, model=args.ai_model, max_files=args.ai_max_files, log=log)
        run_tests = not args.skip_tests
        skip_tests = args.skip_test if run_tests else []
        agent_skills: list[str] = []
        agent_passes: list[bool] = []
        agent_log = None
        if args.agent:
            # Outlives a temp workdir that's deleted on success: the
            # transcript is what you review an agent run by.
            if persistent or args.keep:
                agent_log = workdir / "agent-transcript.log"
            else:
                handle, name = tempfile.mkstemp(prefix="javamod-agent-", suffix=".log")
                os.close(handle)
                agent_log = Path(name)
            agent_skills = agent.install(src_path, args.agent, args.agent_skill, workdir / "agent-skills", log=log)

        def first_prompt(failure: str | None = None) -> str:
            return agent.build_prompt(
                build, src_path, plan.recipe_names if args.engine != "ai" else [], target_java=args.java,
                boot=boot, skills=agent_skills, run_tests=run_tests, skip_tests=skip_tests, failure=failure,
            )
        if args.agent and args.agent_on == "always":
            agent_passes.append(_agent_pass(args, src_path, first_prompt(), agent_log, log))
        if not args.skip_format:
            formatting.reconcile(build, log=log)

        build_result = None
        if not args.skip_build:
            build_result = buildcheck.validate(build, run_tests=run_tests, skip_tests=skip_tests)
            if not build_result.ok and args.engine == "hybrid":
                build_result = ai.fix_build(
                    build, build_result, model=args.ai_model, max_iterations=args.ai_max_iterations,
                    run_tests=run_tests, skip_tests=skip_tests, log=log,
                )
                if not args.skip_format and formatting.reconcile(build, log=log):
                    build_result = buildcheck.validate(build, run_tests=run_tests, skip_tests=skip_tests)
            # Each pass here hands the agent javamod's own failure, not its
            # own view of the build, and javamod re-checks -- the agent never
            # gets to declare the build fixed. With --agent-on failure the
            # first pass happens here too, and only if the recipes fell short.
            remaining = (args.agent_retries + (args.agent_on == "failure")) if args.agent else 0
            for attempt in range(1, remaining + 1):
                if build_result.ok:
                    break
                failure = buildcheck.condense(build.tool, build_result.output)
                if agent_passes:
                    log(f"agent: build check failed; retry {len(agent_passes)}/{args.agent_retries}")
                    prompt = agent.retry_prompt(build, src_path, failure=failure, attempt=len(agent_passes),
                                                skills=agent_skills, skip_tests=skip_tests)
                else:
                    log("agent: build check failed after the recipes; running the agent")
                    prompt = first_prompt(failure)
                agent_passes.append(_agent_pass(args, src_path, prompt, agent_log, log))
                if not args.skip_format:
                    formatting.reconcile(build, log=log)
                build_result = buildcheck.validate(build, run_tests=run_tests, skip_tests=skip_tests)

        message = f"javamod: modernize to Java {args.java}" + (f", Spring Boot {boot}" if boot else "")
        if skip_tests:
            message += "\n\nTests excluded from javamod's build check: " + ", ".join(skip_tests)
        commit = gitrepo.commit_all(src_path, message)
        changed = commit is not None
        diff_stat = gitrepo.diff_stat_since(src_path, base_rev) if changed else ""
        build_ok = build_result.ok if build_result else None
        residual_issues: list[dict] = []
        build_log = None
        if build_ok is False:
            residual_issues = triage.parse_build_failures(build.tool, build_result.output)
            # The full log goes to a file; the summary below shows only the
            # condensed failure (both report paths print it, so a failure is
            # never silent even when --execute blocks the push).
            build_log = workdir / "build-output.log"
            build_log.write_text(build_result.output, encoding="utf-8")
            if not args.quiet and residual_issues:
                with contextlib.redirect_stdout(human_stream):
                    triage.print_summary(residual_issues)
            issues_destination = args.issues or str(workdir / "remaining-issues.yaml")
            triage.write(residual_issues, issues_destination)
            if not args.quiet and issues_destination != "-":
                print(f"\nfull triage written to {issues_destination}", file=human_stream)
        # Built now, before any raise below, so --report still gets written on
        # a failure that blocks the push -- that's precisely when a CI
        # consumer most needs the structured build_ok/residual_issues fields,
        # not only on success.
        report = RunReport(
            source=source, source_ref=ref, build_tool=build.tool,
            build_root=str(build.path.relative_to(src_path)), target_java=args.java,
            boot_target=boot, profile=args.profile, engine=args.engine,
            recipes=plan.recipe_names if args.engine != "ai" else [],
            changed=changed, diff_stat=diff_stat, build_ok=build_ok,
            build_output_tail=buildcheck.condense(build.tool, build_result.output) if build_ok is False else "",
            commit=commit, branch=branch,
            destination=args.dest, pushed=False, residual_issues=residual_issues,
            build_log=str(build_log) if build_log else None, skipped_tests=skip_tests,
            agent=args.agent, agent_ok=all(agent_passes) if agent_passes else None,
            agent_passes=len(agent_passes), agent_skills=agent_skills,
            agent_log=str(agent_log) if agent_log else None,
        )

        if build_ok is False and args.execute and not args.local_only and not args.force_push:
            raise ModError("build/tests failed after migration; not pushing (use --force-push to push anyway)")

        if args.execute and not args.local_only:
            if not args.yes and sys.stdin.isatty():
                print(f"Push branch '{branch}' to {args.dest}? [y/N] ", end="", file=sys.stderr, flush=True)
                reply = input()
                if reply.strip().lower() not in {"y", "yes"}:
                    raise ModError("push cancelled")
            log(f"pushing {branch} -> {args.dest}")
            if args.init_dest and not gitrepo.is_remote(args.dest):
                gitrepo.init_bare_destination(Path(args.dest).expanduser().resolve())
            gitrepo.push(src_path, branch, args.dest, token_env=push_token_env, force=args.force_push)
            report.pushed = True
        success = build_ok is not False
        return 0 if success else 1
    except (ModError, OSError) as exc:
        if report is not None:
            report.error = str(exc)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        cleanup = success and report is not None and report.pushed and not persistent and not args.keep
        if report is not None:
            report.local_checkout = None if cleanup else str(workdir / "src")
            if not args.quiet:
                with contextlib.redirect_stdout(human_stream):
                    report.print_summary(diff_stat_lines=args.diff_stat_lines)
            if args.report:
                report.write_json(args.report)
        if cleanup:
            shutil.rmtree(workdir, ignore_errors=True)
        elif not success:
            print(f"workdir kept for inspection: {workdir}", file=sys.stderr)


def _validate_migrate(args: argparse.Namespace) -> None:
    # argparse validates explicit choices, but not environment-supplied defaults.
    choices = {
        "java": recipes.TARGET_JAVA_VERSIONS, "profile": recipes.PROFILE_DEFAULTS,
        "dependency_strategy": ("patch", "latest"), "recipe_source": recipes.PLUGIN_VERSIONS,
        "engine": ("openrewrite", "hybrid", "ai"), "agent": agent.AGENTS,
        "agent_on": ("always", "failure"), "provider": ("github", "gitlab"),
    }
    for name, allowed in choices.items():
        value = getattr(args, name)
        if value is not None and value not in allowed:
            raise ModError(f"--{name.replace('_', '-')} must be one of {', '.join(map(str, allowed))}")
    for name, minimum in (("ai_max_iterations", 0), ("ai_max_files", 1), ("agent_timeout", 1),
                          ("agent_retries", 0), ("diff_stat_lines", 0)):
        if getattr(args, name) < minimum:
            raise ModError(f"--{name.replace('_', '-')} must be at least {minimum}")
    if not args.local_only and not args.dest:
        raise ModError("--dest is required unless --local-only is set")
    if args.recipe_source == "codegenome" and not args.allow_codegenome:
        raise ModError("--recipe-source codegenome also requires --allow-codegenome")
    if (args.agent_skill or args.agent_retries) and not args.agent:
        raise ModError("--agent-skill and --agent-retries need --agent")
    if args.agent and args.agent_on == "failure" and args.skip_build:
        raise ModError("--agent-on failure needs the build check; drop --skip-build")
    if args.report == "-" and args.issues == "-":
        # Both on stdout would interleave YAML and JSON; the report already
        # carries the same issues as its residual_issues field.
        raise ModError("--report - and --issues - can't both use stdout; the report already includes the issues "
                       "as residual_issues")
    if args.dest_branch.startswith("-") or args.dest_branch == "HEAD":
        raise ModError(f"invalid destination branch: {args.dest_branch!r}")
    gitrepo.run(["git", "check-ref-format", f"refs/heads/{args.dest_branch}"], cwd=Path.cwd())
    if args.agent:
        agent.cli_for(args.agent)  # fail before cloning, not after the recipes


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "doctor":
            return cmd_doctor(args)
        _validate_migrate(args)
        return cmd_migrate(args)
    except (ModError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
