import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from javamod import buildcheck
from javamod.discover import BuildRoot

NOISY_MAVEN = """[INFO] Running com.example.FooTest
2026-10-08T06:46:57.110-06:00  INFO 926699 --- [Thread-9] EmbeddedMongo : {"msg":"Flow Control is enabled"}
[ERROR] Failures:
[ERROR]   FooTest.bar:26 expected: <null> but was: <USD>
[INFO]
[INFO] Reactor Summary:
[INFO] app 1.0 .......................... FAILURE [  1.0 s]
[INFO] BUILD FAILURE
[INFO] Total time:  52.260 s
[ERROR] Failed to execute goal ... There are test failures.
[ERROR] 
[ERROR] -> [Help 1]
[ERROR] Re-run Maven using the -X switch to enable full debug logging.
[ERROR]   mvn <args> -rf :app
"""


class CondenseTests(unittest.TestCase):
    def test_timeout_preserves_partial_byte_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = BuildRoot(path=Path(tmp), tool="maven")
            with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("mvn", 1, output=b"compile failed\n")):
                result = buildcheck.validate(build, run_tests=True, timeout=1)
        self.assertFalse(result.ok)
        self.assertIn("compile failed", result.output)
        self.assertIn("timed out", result.output)

    def test_maven_keeps_errors_and_reactor_summary_and_drops_log_noise(self):
        text = buildcheck.condense("maven", NOISY_MAVEN)
        self.assertIn("FooTest.bar:26", text)
        self.assertIn("app 1.0 .......................... FAILURE", text)
        self.assertIn("Failed to execute goal", text)
        self.assertNotIn("EmbeddedMongo", text)
        self.assertNotIn("Total time", text)
        self.assertNotIn("Re-run Maven", text)
        self.assertNotIn("-rf :app", text)

    def test_falls_back_to_raw_output_when_nothing_matches(self):
        self.assertEqual(buildcheck.condense("maven", "mvn: not found"), "mvn: not found")


class SkipTestsTests(unittest.TestCase):
    def _command(self, tool: str, skip_tests):
        with tempfile.TemporaryDirectory() as tmp:
            build = BuildRoot(path=Path(tmp), tool=tool)
            seen = {}

            def fake_run(cmd, **_kwargs):
                flag = next((a for a in cmd if a.startswith("-Dsurefire.excludesFile=")), None)
                path = flag.split("=", 1)[1] if flag else (cmd[cmd.index("--init-script") + 1]
                                                             if "--init-script" in cmd else None)
                seen["content"] = Path(path).read_text() if path else None
                return mock.Mock(returncode=0, stdout="")
            with mock.patch("subprocess.run", side_effect=fake_run) as run:
                buildcheck.validate(build, run_tests=True, skip_tests=skip_tests)
            return run.call_args.args[0], seen["content"]

    def test_maven_uses_an_additive_surefire_excludes_file(self):
        cmd, content = self._command("maven", ["LiveApiTest", "com.example.OtherTest"])
        self.assertTrue(any(a.startswith("-Dsurefire.excludesFile=") for a in cmd))
        self.assertFalse(any(a.startswith("-Dtest=") for a in cmd))
        self.assertEqual(content, "**/LiveApiTest.java\ncom/example/OtherTest.java\n")

    def test_gradle_uses_an_init_script_with_test_excludes(self):
        cmd, content = self._command("gradle", ["LiveApiTest"])
        self.assertIn("--init-script", cmd)
        self.assertIn("exclude '**/LiveApiTest.class', '**/LiveApiTest$*.class'", content)

    def test_no_skips_adds_nothing(self):
        cmd, content = self._command("maven", [])
        self.assertEqual(cmd[-1], "test")
        self.assertIsNone(content)


if __name__ == "__main__":
    unittest.main()
