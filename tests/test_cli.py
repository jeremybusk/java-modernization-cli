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


if __name__ == "__main__":
    unittest.main()
