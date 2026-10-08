import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from javamod import cli

POM_JAVA_8 = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId><artifactId>demo</artifactId><version>1.0</version>
  <properties><maven.compiler.release>8</maven.compiler.release></properties>
</project>
"""

POM_SPRING_BOOT_2_3 = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>2.3.0.RELEASE</version>
  </parent>
  <groupId>com.example</groupId><artifactId>demo</artifactId><version>1.0</version>
  <properties><maven.compiler.release>11</maven.compiler.release></properties>
  <dependencies>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter</artifactId></dependency>
  </dependencies>
</project>
"""


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_source_repo(root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    (source / "pom.xml").write_text(POM_JAVA_8, encoding="utf-8")
    git("init", "-q", "-b", "main", cwd=source)
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=source)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=source)
    return source


class MigrateArgumentValidationTests(unittest.TestCase):
    def test_dest_required_unless_local_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source_repo(Path(tmp))
            code = cli.main(["migrate", "--source", str(source), "--dest-branch", "modernize-java21"])
            self.assertEqual(code, 2)


class AgentArgumentValidationTests(unittest.TestCase):
    def test_report_and_issues_cannot_both_use_stdout(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(["migrate", "--source", ".", "--dest-branch", "b", "--local-only",
                             "--report", "-", "--issues", "-"])
        self.assertEqual(code, 2)
        self.assertIn("residual_issues", err.getvalue())

    def test_agent_on_failure_needs_the_build(self):
        with mock.patch("javamod.agent.cli_for"), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["migrate", "--source", ".", "--dest-branch", "b", "--local-only",
                             "--agent", "claude", "--agent-on", "failure", "--skip-build"])
        self.assertEqual(code, 2)

    @mock.patch("javamod.openrewrite.run", return_value=True)
    def test_boot_target_without_spring_boot_warns(self, _mock_rewrite):
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source_repo(Path(tmp))
            with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["migrate", "--source", str(source), "--dest-branch", "b", "--local-only",
                                 "--skip-build", "--boot", "3.5", "--workdir", str(Path(tmp) / "w")])
        self.assertEqual(code, 0)
        self.assertIn("--boot 3.5 ignored", err.getvalue())

    def test_agent_skill_requires_agent(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["migrate", "--source", ".", "--dest-branch", "b", "--local-only",
                             "--agent-skill", "modern-java"])
        self.assertEqual(code, 2)

    def test_missing_agent_cli_fails_before_cloning(self):
        with mock.patch("shutil.which", return_value=None), mock.patch("javamod.gitrepo.clone_source") as clone:
            with contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["migrate", "--source", ".", "--dest-branch", "b", "--local-only", "--agent", "codex"])
        self.assertEqual(code, 2)
        clone.assert_not_called()


def _fake_agent_edit(repo, _prompt, **_kwargs):
    (repo / "Edited.java").write_text("class Edited {}", encoding="utf-8")
    return True


class AgentRetryTests(unittest.TestCase):
    def _migrate(self, tmp: Path, results: list, retries: int, extra=()):
        source = make_source_repo(tmp)
        validate = mock.Mock(side_effect=[mock.Mock(ok=ok, output="[ERROR]   FooTest.bar:3 boom") for ok in results])
        with mock.patch("javamod.openrewrite.run", return_value=True), \
                mock.patch("javamod.agent.cli_for"), \
                mock.patch("javamod.agent.install", return_value=[]), \
                mock.patch("javamod.agent.run", side_effect=_fake_agent_edit) as run, \
                mock.patch("javamod.buildcheck.validate", validate), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["migrate", "--source", str(source), "--dest-branch", "b", "--local-only",
                             "--workdir", str(tmp / "work"), "--agent", "claude",
                             "--agent-retries", str(retries), "--skip-format", *extra])
        return code, run, validate

    def test_retries_until_javamods_own_check_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, run, validate = self._migrate(Path(tmp), [False, False, True], retries=3)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 3)  # first pass + 2 retries; stops once the check passes
        self.assertEqual(validate.call_count, 3)
        self.assertIn("FooTest.bar:3 boom", run.call_args.args[1])

    def test_no_retries_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, run, _validate = self._migrate(Path(tmp), [False], retries=0)
        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 1)

    def test_agent_on_failure_skips_the_agent_when_recipes_suffice(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, run, _validate = self._migrate(Path(tmp), [True], retries=2, extra=("--agent-on", "failure"))
        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_agent_on_failure_runs_with_the_failure_then_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, run, _validate = self._migrate(Path(tmp), [False, False, True], retries=1,
                                                 extra=("--agent-on", "failure"))
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 2)  # first pass on failure + 1 retry
        first, retry = (c.args[1] for c in run.call_args_list)
        self.assertIn("currently fails", first)
        self.assertIn("Follow-up pass 1", retry)

    def test_skip_test_reaches_the_build_check_and_the_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _run, validate = self._migrate(Path(tmp), [True], retries=0, extra=("--skip-test", "LiveApiTest"))
            message = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=Path(tmp) / "work" / "src",
                                     capture_output=True, text=True, check=True).stdout
        self.assertEqual(code, 0)
        self.assertEqual(validate.call_args.kwargs["skip_tests"], ["LiveApiTest"])
        self.assertIn("Tests excluded from javamod's build check: LiveApiTest", message)


class MigrateDryRunTests(unittest.TestCase):
    @mock.patch("javamod.openrewrite.run", return_value=True)
    def test_local_only_skip_build_run_succeeds_and_creates_branch(self, _mock_rewrite):
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source_repo(Path(tmp))
            workdir = Path(tmp) / "work"
            code = cli.main([
                "migrate", "--source", str(source), "--dest-branch", "modernize-java21",
                "--local-only", "--skip-build", "--java", "21", "--workdir", str(workdir),
            ])
            self.assertEqual(code, 0)
            branch = subprocess.run(["git", "branch", "--show-current"], cwd=workdir / "src",
                                     capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(branch, "modernize-java21")

    @mock.patch("javamod.buildcheck.validate")
    @mock.patch("javamod.openrewrite.run", return_value=False)
    def test_execute_without_yes_and_without_tty_does_not_hang(self, _mock_rewrite, mock_validate):
        mock_validate.return_value = mock.Mock(ok=True, output="")
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source_repo(Path(tmp))
            dest = Path(tmp) / "dest.git"
            from javamod import gitrepo
            gitrepo.init_bare_destination(dest)
            code = cli.main([
                "migrate", "--source", str(source), "--dest", str(dest), "--dest-branch", "modernize-java21",
                "--execute", "--yes", "--java", "21",
            ])
            self.assertEqual(code, 0)


class BuildFailureDiagnosticsTests(unittest.TestCase):
    """Regression coverage for a real failure: a build failure whose shape
    triage doesn't recognize must still print *something* -- especially
    since --execute raises right after this and never reaches the full
    report/raw build output otherwise.
    """

    @mock.patch("javamod.openrewrite.run", return_value=True)
    def test_unmatched_failure_still_prints_raw_output_with_execute(self, _mock_rewrite):
        unrecognized_output = "[ERROR] something went wrong in a shape triage has never seen"
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source_repo(Path(tmp))
            dest = Path(tmp) / "dest.git"
            from javamod import gitrepo
            gitrepo.init_bare_destination(dest)
            buffer = io.StringIO()
            with mock.patch("javamod.buildcheck.validate",
                             return_value=mock.Mock(ok=False, output=unrecognized_output)), \
                 contextlib.redirect_stdout(buffer):
                code = cli.main([
                    "migrate", "--source", str(source), "--dest", str(dest), "--dest-branch", "modernize-java21",
                    "--execute", "--yes", "--java", "21",
                ])
            self.assertEqual(code, 2)
            self.assertIn(unrecognized_output, buffer.getvalue())

    @mock.patch("javamod.buildcheck.validate")
    @mock.patch("javamod.openrewrite.run", return_value=True)
    def test_matched_failure_writes_issues_file_even_with_execute(self, _mock_rewrite, mock_validate):
        mock_validate.return_value = mock.Mock(
            ok=False, output="[ERROR] /app/Service.java:[1,1] cannot find symbol\n"
                             "[ERROR]   symbol:   method findOne(long)\n")
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source_repo(Path(tmp))
            dest = Path(tmp) / "dest.git"
            from javamod import gitrepo
            gitrepo.init_bare_destination(dest)
            workdir = Path(tmp) / "work"
            code = cli.main([
                "migrate", "--source", str(source), "--dest", str(dest), "--dest-branch", "modernize-java21",
                "--execute", "--yes", "--java", "21", "--workdir", str(workdir),
            ])
            self.assertEqual(code, 2)
            issues_file = workdir / "remaining-issues.yaml"
            self.assertTrue(issues_file.is_file())
            self.assertIn("renamed-api", issues_file.read_text(encoding="utf-8"))

    @mock.patch("javamod.buildcheck.validate")
    @mock.patch("javamod.openrewrite.run", return_value=True)
    def test_report_is_still_written_when_execute_blocks_on_a_failed_build(self, _mock_rewrite, mock_validate):
        # The exact bug reported: --report - produced nothing on a build
        # failure that blocked the push, even though the whole point of
        # --report is for a CI consumer to see build_ok/residual_issues
        # precisely when the build failed.
        mock_validate.return_value = mock.Mock(
            ok=False, output="[ERROR] /app/Service.java:[1,1] cannot find symbol\n"
                             "[ERROR]   symbol:   method findOne(long)\n")
        with tempfile.TemporaryDirectory() as tmp:
            source = make_source_repo(Path(tmp))
            dest = Path(tmp) / "dest.git"
            from javamod import gitrepo
            gitrepo.init_bare_destination(dest)
            report_path = Path(tmp) / "report.json"
            code = cli.main([
                "migrate", "--source", str(source), "--dest", str(dest), "--dest-branch", "modernize-java21",
                "--execute", "--yes", "--java", "21", "--report", str(report_path),
            ])
            self.assertEqual(code, 2)
            self.assertTrue(report_path.is_file())
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(payload["build_ok"])
            self.assertFalse(payload["pushed"])
            self.assertTrue(any(i["category"] == "renamed-api" for i in payload["residual_issues"]))


class BootCrossMajorJumpCliTests(unittest.TestCase):
    @mock.patch("javamod.buildcheck.validate", return_value=mock.Mock(ok=True, output=""))
    @mock.patch("javamod.openrewrite.run", return_value=True)
    def test_cross_major_boot_jump_runs_in_a_single_pass(self, mock_rewrite, _mock_validate):
        # A jump from the detected 2.3 to a 3.5 target needs no staging --
        # OpenRewrite's own recipe chains back through the major boundary
        # itself (see recipes.spring_boot_recipe's docstring).
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "pom.xml").write_text(POM_SPRING_BOOT_2_3, encoding="utf-8")
            git("init", "-q", "-b", "main", cwd=source)
            git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=source)
            git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=source)
            code = cli.main([
                "migrate", "--source", str(source), "--dest-branch", "modernize-java21",
                "--local-only", "--skip-build", "--java", "21", "--boot", "3.5",
            ])
            self.assertEqual(code, 0)
            mock_rewrite.assert_called_once()
            applied = mock_rewrite.call_args.args[1].recipe_names
            self.assertIn("org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_5", applied)


class EnvVarDefaultTests(unittest.TestCase):
    def test_java_target_reads_env_var(self):
        with mock.patch.dict("os.environ", {"JAVAMOD_JAVA": "17"}):
            parser = cli.build_parser()
            args = parser.parse_args(["migrate", "--source", "x", "--dest-branch", "b", "--local-only"])
            self.assertEqual(args.java, 17)

    def test_cli_flag_overrides_env_var(self):
        with mock.patch.dict("os.environ", {"JAVAMOD_JAVA": "17"}):
            parser = cli.build_parser()
            args = parser.parse_args(["migrate", "--source", "x", "--dest-branch", "b", "--local-only", "--java", "25"])
            self.assertEqual(args.java, 25)

    def test_default_java_target_is_21_with_no_env_or_flag(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            parser = cli.build_parser()
            args = parser.parse_args(["migrate", "--source", "x", "--dest-branch", "b", "--local-only"])
            self.assertEqual(args.java, 21)


class AutomationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = make_source_repo(self.root)
        self.workdir = self.root / "work"

    def migrate(self, *extra, temporary=False):
        argv = ["migrate", "--source", str(self.source), "--dest-branch", "modernize",
                "--local-only", "--skip-build", "--report", "-", *extra]
        if not temporary:
            argv += ["--workdir", str(self.workdir)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("javamod.openrewrite.run", return_value=False), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_verbose_json_is_clean_without_quiet(self):
        code, payload, diagnostics = self.migrate("--verbose")
        self.assertEqual(code, 0)
        self.assertIn("[javamod] cloning", diagnostics)
        self.assertIn("source:", diagnostics)
        self.assertEqual(payload["local_checkout"], str(self.workdir / "src"))

    def test_mixed_builds_fail_before_recipes_run(self):
        (self.source / "build.gradle").write_text("plugins { id 'java' }")
        from javamod import gitrepo
        gitrepo.commit_all(self.source, "add Gradle build")
        stderr = io.StringIO()
        with mock.patch("javamod.openrewrite.run") as rewrite, contextlib.redirect_stderr(stderr):
            code = cli.main(["migrate", "--source", str(self.source), "--dest-branch", "b",
                             "--local-only", "--workdir", str(self.workdir)])
        self.assertEqual(code, 2)
        self.assertIn("--build-tool gradle", stderr.getvalue())
        rewrite.assert_not_called()

    def test_selected_gradle_reports_alternate_pom_even_when_quiet(self):
        (self.source / "build.gradle").write_text("plugins { id 'java' }")
        (self.source / "pom.xml").write_text("invalid alternate POM")
        from javamod import gitrepo
        gitrepo.commit_all(self.source, "add Gradle build")
        code, payload, _ = self.migrate("--build-tool", "gradle", "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(payload["build_tool"], "gradle")
        self.assertIn("pom.xml", payload["build_warnings"][0])
        self.assertEqual((self.workdir / "src/pom.xml").read_text(), "invalid alternate POM")

    def test_successful_temporary_local_checkout_is_retained(self):
        with mock.patch("javamod.cli.tempfile.mkdtemp", return_value=str(self.workdir)):
            code, payload, _ = self.migrate(temporary=True)
        self.assertEqual(code, 0)
        self.assertTrue(Path(payload["local_checkout"]).is_dir())

    def test_report_survives_push_failure(self):
        from javamod.errors import ModError
        stdout = io.StringIO()
        with mock.patch("javamod.openrewrite.run", return_value=False), \
                mock.patch("javamod.gitrepo.push", side_effect=ModError("destination rejected push")), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["migrate", "--source", str(self.source), "--dest-branch", "b",
                             "--dest", "unused", "--execute", "--yes", "--skip-build", "--report", "-",
                             "--workdir", str(self.root / "push-work")])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(payload["pushed"])
        self.assertIn("destination rejected push", payload["error"])
        self.assertTrue(Path(payload["local_checkout"]).is_dir())

    def test_successful_push_cleans_only_its_temporary_checkout(self):
        dest = self.root / "dest.git"
        stdout = io.StringIO()
        with mock.patch("javamod.openrewrite.run", return_value=False), \
                mock.patch("javamod.cli.tempfile.mkdtemp", return_value=str(self.workdir)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["migrate", "--source", str(self.source), "--dest-branch", "b",
                             "--dest", str(dest), "--init-dest", "--execute", "--yes", "--skip-build", "--report", "-"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["pushed"])
        self.assertIsNone(payload["local_checkout"])
        self.assertFalse(self.workdir.exists())
        self.assertTrue((dest / "refs/heads/b").is_file())

    def test_cancelled_push_writes_report(self):
        stdout = io.StringIO()
        with mock.patch("javamod.openrewrite.run", return_value=False), \
                mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="no"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["migrate", "--source", str(self.source), "--dest-branch", "b",
                             "--dest", "unused", "--execute", "--skip-build", "--report", "-",
                             "--workdir", str(self.workdir)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["error"], "push cancelled")

    def test_hybrid_fixes_are_formatted_and_validated_again(self):
        from javamod.buildcheck import BuildResult
        failed, passed = BuildResult(False, [], "compile failed"), BuildResult(True, [], "")
        stdout = io.StringIO()
        with mock.patch("javamod.openrewrite.run", return_value=False), \
                mock.patch("javamod.buildcheck.validate", side_effect=[failed, passed]) as validate, \
                mock.patch("javamod.ai.fix_build", return_value=failed), \
                mock.patch("javamod.formatting.reconcile", return_value=True) as formatter, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["migrate", "--source", str(self.source), "--dest-branch", "b", "--local-only",
                             "--engine", "hybrid", "--report", "-", "--workdir", str(self.workdir)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["build_ok"])
        self.assertEqual(formatter.call_count, 2)
        self.assertEqual(validate.call_count, 2)

    def test_local_only_execute_build_failure_does_not_claim_a_blocked_push(self):
        with mock.patch("javamod.buildcheck.validate", return_value=mock.Mock(ok=False, output="bad build")):
            stdout = io.StringIO()
            with mock.patch("javamod.openrewrite.run", return_value=False), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["migrate", "--source", str(self.source), "--dest-branch", "b",
                                 "--local-only", "--execute", "--quiet", "--report", "-",
                                 "--workdir", str(self.workdir)])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout.getvalue())["build_ok"])

    def test_init_dest_does_not_create_destination_during_local_run(self):
        dest = self.root / "dest.git"
        code, _, _ = self.migrate("--init-dest", "--dest", str(dest))
        self.assertEqual(code, 0)
        self.assertFalse(dest.exists())

    def test_existing_destination_branch_preserves_source_ref(self):
        git("switch", "-c", "feature", cwd=self.source)
        (self.source / "pom.xml").write_text(POM_JAVA_8.replace(">8<", ">17<"), encoding="utf-8")
        git("add", "-A", cwd=self.source)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "feature", cwd=self.source)
        git("switch", "main", cwd=self.source)
        code, _, _ = self.migrate("--source-ref", "feature", "--dest-branch", "main")
        self.assertEqual(code, 0)
        self.assertIn(">17<", (self.workdir / "src/pom.xml").read_text())

    def test_ai_report_does_not_claim_recipes_were_applied(self):
        with mock.patch("javamod.ai.modernize_tree", return_value=[]):
            code, payload, _ = self.migrate("--engine", "ai")
        self.assertEqual(code, 0)
        self.assertEqual(payload["recipes"], [])

    def test_ignored_boot_target_is_not_reported_as_applied(self):
        code, payload, _ = self.migrate("--boot", "3.5")
        self.assertEqual(code, 0)
        self.assertIsNone(payload["boot_target"])

    def test_invalid_env_defaults_fail_before_cloning(self):
        for variable, value in (("JAVAMOD_JAVA", "abc"), ("JAVAMOD_JAVA", "99"),
                                ("JAVAMOD_ENGINE", "unknown"), ("JAVAMOD_ALLOW_CODEGENOME", "maybe"),
                                ("JAVAMOD_BUILD_TOOL", "unknown")):
            with self.subTest(variable=variable, value=value), \
                    mock.patch.dict("os.environ", {variable: value}), \
                    mock.patch("javamod.gitrepo.clone_source") as clone, \
                    contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["migrate", "--source", str(self.source), "--dest-branch", "b", "--local-only"])
                self.assertEqual(code, 2)
                clone.assert_not_called()

    def test_invalid_limits_and_branch_fail_before_cloning(self):
        for flag, value in (("--agent-retries", "-1"), ("--ai-max-files", "0"),
                            ("--agent-timeout", "0"), ("--dest-branch", "bad..name")):
            with self.subTest(flag=flag), mock.patch("javamod.gitrepo.clone_source") as clone, \
                    contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["migrate", "--source", str(self.source), "--dest-branch", "b", "--local-only",
                                 flag, value])
                self.assertEqual(code, 2)
                clone.assert_not_called()


if __name__ == "__main__":
    unittest.main()
