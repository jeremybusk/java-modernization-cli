import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from javamod import agent
from javamod.discover import BuildRoot
from javamod.errors import ModError


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
    git("init", "-q", "-b", "main", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def make_skill(root: Path, name: str) -> Path:
    skill = root / f"skill-src-{name}"
    skill.mkdir()
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test\n---\nbody\n", encoding="utf-8")
    return skill


def fake_cli(root: Path, script: str) -> Path:
    """An executable named 'claude' on a private PATH that runs *script* in the repo."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return bin_dir


class ResolveSkillTests(unittest.TestCase):
    def test_remote_skill_cannot_escape_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("javamod.agent._fetch"):
            with self.assertRaisesRegex(ModError, "within its repository"):
                agent.resolve_skill("https://example.com/skill.git#../../outside", Path(tmp))

    def test_installation_preserves_existing_project_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_repo(root)
            source = make_skill(root, "demo")
            target = repo / ".claude/skills/demo"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("original")
            with self.assertRaisesRegex(ModError, "already exists"):
                agent.install_skills(repo, agent.AGENTS["claude"], [str(source)], root / "cache")
            self.assertEqual((target / "SKILL.md").read_text(), "original")

    def test_invalid_skill_name_cannot_escape_installation_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_skill(root, "demo")
            (source / "SKILL.md").write_text("---\nname: ..\n---\nbody")
            with self.assertRaisesRegex(ModError, "invalid skill name"):
                agent.install_skills(root, agent.AGENTS["claude"], [str(source)], root / "cache")

    def test_local_directory_with_skill_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = make_skill(Path(tmp), "demo")
            self.assertEqual(agent.resolve_skill(str(skill), Path(tmp) / "cache"), skill.resolve())
            self.assertEqual(agent.skill_name(skill), "demo")

    def test_directory_without_skill_md_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ModError):
                agent.resolve_skill(tmp, Path(tmp) / "cache")

    def test_unknown_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ModError):
                agent.resolve_skill("no-such-skill", Path(tmp) / "cache")

    def test_builtins_are_pinned_to_a_commit(self):
        for url, commit, _subdir in agent.BUILTIN_SKILLS.values():
            self.assertRegex(commit, r"^[0-9a-f]{40}$", url)


class BuildPromptTests(unittest.TestCase):
    def test_mentions_target_recipes_skills_and_forbids_committing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            build = BuildRoot(path=repo, tool="maven", current_java=8, features=set())
            prompt = agent.build_prompt(build, repo, ["org.openrewrite.java.migrate.UpgradeToJava21"],
                                        target_java=21, boot="3.5", skills=["modern-java"], run_tests=True)
        self.assertIn("Java 21 and Spring Boot 3.5", prompt)
        self.assertIn("UpgradeToJava21", prompt)
        self.assertIn("modern-java", prompt)
        self.assertIn("Do NOT run git commit", prompt)


class RetryPromptTests(unittest.TestCase):
    def test_carries_failure_skips_and_test_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            build = BuildRoot(path=repo, tool="maven")
            prompt = agent.retry_prompt(build, repo, failure="[ERROR]   FooTest.bar:3 boom", attempt=2,
                                        skills=["modern-java"], skip_tests=["LiveApiTest"])
        self.assertIn("Follow-up pass 2", prompt)
        self.assertIn("FooTest.bar:3 boom", prompt)
        self.assertIn("LiveApiTest", prompt)
        self.assertIn("Never delete, disable", prompt)


class RunTests(unittest.TestCase):
    def _run(self, tmp: Path, script: str, skills=()):
        repo = make_repo(tmp)
        bin_dir = fake_cli(tmp, script)
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
            installed = agent.install(repo, "claude", list(skills), tmp / "cache", log=lambda _m: None)
            ok = agent.run(repo, "do it", agent_name="claude", model=None, extra_args=[], timeout=60,
                           log_path=tmp / "transcript.log", log=lambda _m: None)
        return repo, ok, installed

    def test_edits_are_kept_and_skills_are_excluded_from_the_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            skill = make_skill(tmp, "demo")
            repo, ok, installed = self._run(tmp, "echo changed > Changed.java\necho done\n", skills=[str(skill)])
            self.assertTrue(ok)
            self.assertEqual(installed, ["demo"])
            self.assertTrue((repo / ".claude/skills/demo/SKILL.md").is_file())
            status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                                    text=True, check=True).stdout
            self.assertIn("Changed.java", status)
            self.assertNotIn(".claude", status)
            self.assertIn("done", (tmp / "transcript.log").read_text())

    def test_commits_made_by_the_agent_are_folded_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            script = ("echo x > A.java && git add A.java && "
                      "git -c user.email=a@a -c user.name=a commit -q -m agent\n")
            repo, _ok, _installed = self._run(tmp, script)
            log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True,
                                 check=True).stdout
            self.assertEqual(len(log.splitlines()), 1)
            self.assertTrue((repo / "A.java").is_file())

    def test_nonzero_exit_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            _repo, ok, _installed = self._run(Path(tmp), "exit 3\n")
            self.assertFalse(ok)

    def test_missing_cli_fails_fast(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(ModError):
                agent.cli_for("codex")


if __name__ == "__main__":
    unittest.main()
