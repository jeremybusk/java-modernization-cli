"""The ``javamod`` command line: ``javamod migrate`` and ``javamod doctor``.

Design goal: one command, sensible defaults, repeatable across many repos
with no config file. Every tunable below also reads a ``JAVAMOD_*``
environment variable, so a team can export its target Java/Boot
version/profile/recipe-source once and then run the bare command the same
way against every repository.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.parse
from pathlib import Path

from . import ai, buildcheck, discover, formatting, gitrepo, openrewrite, recipes, triage
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

    migrate.add_argument("--skip-build", action="store_true", help="skip compiling/testing the result")
    migrate.add_argument("--skip-format", action="store_true",
                          help="don't run the project's own formatter (spring-javaformat/Spotless) after migrating, "
                               "even if one is detected")
    migrate.add_argument("--skip-tests", action="store_true", help="compile only; don't run the test suite")
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
    ok = bool(found["git"] and found["java"] and found["javac"] and (found["mvn"] or found["gradle"])) and ai_ok
    if not ok:
        print("\nOn Ubuntu/Debian:\n  sudo apt-get update && sudo apt-get install -y git openjdk-21-jdk maven gradle")
        print("\n(or open this repo in its devcontainer, which ships all of the above)")
    return 0 if ok else 1


def cmd_migrate(args: argparse.Namespace) -> int:
    if not args.local_only and not args.dest:
        raise ModError("--dest is required unless --local-only is set")
    source, inline_ref = gitrepo.split_ref(args.source)
    ref = args.source_ref or inline_ref
    log = (lambda msg: print(f"[javamod] {msg}")) if args.verbose else (lambda _msg: None)

    persistent = args.workdir is not None
    workdir = (args.workdir or Path(tempfile.mkdtemp(prefix="javamod-"))).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    push_token_env = _resolve_token_env(args)
    success = False
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

        branch = args.dest_branch
        gitrepo.ensure_branch(src_path, branch)

        plan = recipes.build_plan(
            build, target_java=args.java, boot=args.boot, profile=args.profile,
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
        if not args.skip_format:
            formatting.reconcile(build, log=log)

        build_result = None
        if not args.skip_build:
            build_result = buildcheck.validate(build, run_tests=not args.skip_tests)
            if not build_result.ok and args.engine == "hybrid":
                build_result = ai.fix_build(
                    build, build_result, model=args.ai_model, max_iterations=args.ai_max_iterations,
                    run_tests=not args.skip_tests, log=log,
                )

        message = f"javamod: modernize to Java {args.java}" + (f", Spring Boot {args.boot}" if args.boot else "")
        commit = gitrepo.commit_all(src_path, message)
        changed = commit is not None
        diff_stat = gitrepo.diff_stat_since(src_path, base_rev) if changed else ""
        build_ok = build_result.ok if build_result else None
        residual_issues: list[dict] = []
        if build_ok is False:
            residual_issues = triage.parse_build_failures(build.tool, build_result.output)
            if not args.quiet:
                if residual_issues:
                    triage.print_summary(residual_issues)
                else:
                    # No known pattern matched -- still show *something* rather
                    # than nothing, especially since --execute raises right after
                    # this and never reaches the full report/build-output print.
                    print("\nbuild/test FAILED; no known issue pattern matched this failure. Last output:")
                    print(build_result.output[-3000:])
            issues_destination = args.issues or str(workdir / "remaining-issues.yaml")
            triage.write(residual_issues, issues_destination)
            if not args.quiet and issues_destination != "-":
                print(f"\nfull triage written to {issues_destination}")
        if build_ok is False and args.execute and not args.force_push:
            raise ModError("build/tests failed after migration; not pushing (use --force-push to push anyway)")

        pushed = False
        if args.execute and not args.local_only:
            if not args.yes and sys.stdin.isatty():
                reply = input(f"Push branch '{branch}' to {args.dest}? [y/N] ")
                if reply.strip().lower() not in {"y", "yes"}:
                    raise ModError("push cancelled")
            log(f"pushing {branch} -> {args.dest}")
            gitrepo.push(src_path, branch, args.dest, token_env=push_token_env, force=args.force_push)
            pushed = True

        report = RunReport(
            source=source, source_ref=ref, build_tool=build.tool,
            build_root=str(build.path.relative_to(src_path)), target_java=args.java,
            boot_target=args.boot, profile=args.profile, engine=args.engine, recipes=plan.recipe_names,
            changed=changed, diff_stat=diff_stat, build_ok=build_ok,
            build_output_tail=build_result.output if build_result else "", commit=commit, branch=branch,
            destination=args.dest, pushed=pushed, residual_issues=residual_issues,
        )
        if not args.quiet:
            report.print_summary()
            if not pushed:
                print(f"local checkout: {src_path}")
        if args.report:
            report.write_json(args.report)
        success = build_ok is not False
        return 0 if success else 1
    except ModError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if success and not persistent and not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        elif not success:
            print(f"workdir kept for inspection: {workdir}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "migrate" and args.recipe_source == "codegenome" and not args.allow_codegenome:
        print("error: --recipe-source codegenome also requires --allow-codegenome", file=sys.stderr)
        return 2
    if args.command == "migrate" and args.init_dest and args.dest and not gitrepo.is_remote(args.dest):
        gitrepo.init_bare_destination(Path(args.dest).expanduser().resolve())
    try:
        return cmd_migrate(args)
    except ModError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
