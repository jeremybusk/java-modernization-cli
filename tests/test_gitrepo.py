import subprocess
import tempfile
import unittest
from pathlib import Path

from javamod import gitrepo
from javamod.errors import ModError


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "init", cwd=path)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add readme", cwd=path)


class LocationParsingTests(unittest.TestCase):
    def test_is_remote_detects_ssh_and_https(self):
        self.assertTrue(gitrepo.is_remote("git@github.com:acme/app.git"))
        self.assertTrue(gitrepo.is_remote("https://github.com/acme/app.git"))

    def test_is_remote_false_for_local_paths(self):
        self.assertFalse(gitrepo.is_remote("/home/me/app"))
        self.assertFalse(gitrepo.is_remote("./relative/app"))

    def test_split_ref_splits_trailing_fragment(self):
        self.assertEqual(gitrepo.split_ref("git@github.com:acme/app.git#release/9"),
                          ("git@github.com:acme/app.git", "release/9"))

    def test_split_ref_leaves_plain_location_alone(self):
        self.assertEqual(gitrepo.split_ref("/home/me/app"), ("/home/me/app", None))


class CloneSourceTests(unittest.TestCase):
    def test_plain_source_rejects_workdir_inside_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            with self.assertRaisesRegex(ModError, "outside a plain source"):
                gitrepo.clone_source(str(root), None, workdir, allow_dirty=False, shallow=False, token_env=None)
            self.assertFalse((workdir / "src").exists())

    def test_explicit_local_tag_is_available_with_no_tags_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            init_repo(source)
            git("tag", "release", cwd=source)
            expected = gitrepo.capture(["git", "rev-parse", "release"], source)
            (source / "README.md").write_text("newer")
            gitrepo.commit_all(source, "newer")
            workdir = root / "work"
            workdir.mkdir()
            clone = gitrepo.clone_source(str(source), "release", workdir,
                                         allow_dirty=False, shallow=False, token_env=None)
            self.assertEqual(gitrepo.capture(["git", "rev-parse", "HEAD"], clone), expected)

    def test_shallow_remote_ref_does_not_clone_full_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            init_repo(source)
            workdir = root / "work"
            workdir.mkdir()
            clone = gitrepo.clone_source(source.as_uri(), "main", workdir,
                                         allow_dirty=False, shallow=True, token_env=None)
            self.assertEqual(gitrepo.capture(["git", "rev-parse", "--is-shallow-repository"], clone), "true")
            self.assertEqual(gitrepo.capture(["git", "rev-list", "--count", "HEAD"], clone), "1")

    def test_clones_a_local_git_repo_at_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            init_repo(source)
            workdir = root / "work"
            workdir.mkdir()
            cloned = gitrepo.clone_source(str(source), None, workdir, allow_dirty=False, shallow=False, token_env=None)
            self.assertTrue((cloned / "README.md").is_file())

    def test_clones_at_an_explicit_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            init_repo(source)
            git("checkout", "-q", "-b", "feature", cwd=source)
            (source / "feature-only.txt").write_text("x\n", encoding="utf-8")
            git("add", "-A", cwd=source)
            git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "feature", cwd=source)

            workdir = root / "work"
            workdir.mkdir()
            cloned = gitrepo.clone_source(str(source), "main", workdir, allow_dirty=False, shallow=False, token_env=None)
            self.assertFalse((cloned / "feature-only.txt").exists())

    def test_rejects_dirty_local_source_without_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            init_repo(source)
            (source / "README.md").write_text("dirty\n", encoding="utf-8")
            workdir = root / "work"
            workdir.mkdir()
            with self.assertRaises(ModError):
                gitrepo.clone_source(str(source), None, workdir, allow_dirty=False, shallow=False, token_env=None)

    def test_plain_directory_without_git_is_snapshotted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "plain"
            source.mkdir()
            (source / "App.java").write_text("class App {}\n", encoding="utf-8")
            workdir = root / "work"
            workdir.mkdir()
            cloned = gitrepo.clone_source(str(source), None, workdir, allow_dirty=False, shallow=False, token_env=None)
            self.assertTrue((cloned / "App.java").is_file())
            self.assertTrue((cloned / ".git").exists())


class CommitAndPushTests(unittest.TestCase):
    def test_commit_all_returns_none_when_nothing_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            init_repo(repo)
            self.assertIsNone(gitrepo.commit_all(repo, "no-op"))

    def test_commit_all_returns_sha_when_something_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            init_repo(repo)
            (repo / "new.txt").write_text("x\n", encoding="utf-8")
            sha = gitrepo.commit_all(repo, "add new.txt")
            self.assertIsNotNone(sha)

    def test_push_to_local_bare_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_repo(repo)
            gitrepo.ensure_branch(repo, "modernize-java21")
            (repo / "new.txt").write_text("x\n", encoding="utf-8")
            gitrepo.commit_all(repo, "modernize")

            bare = root / "dest.git"
            gitrepo.init_bare_destination(bare)
            gitrepo.push(repo, "modernize-java21", str(bare), token_env=None)

            out = subprocess.run(["git", "branch", "--list", "modernize-java21"], cwd=bare,
                                  capture_output=True, text=True, check=True)
            self.assertIn("modernize-java21", out.stdout)

    def test_ignore_build_outputs_keeps_target_dir_out_of_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            init_repo(repo)
            gitrepo.ignore_build_outputs(repo, repo, "maven")
            (repo / "target" / "classes").mkdir(parents=True)
            (repo / "target" / "classes" / "Demo.class").write_bytes(b"fake")
            (repo / "src.txt").write_text("real change\n", encoding="utf-8")
            sha = gitrepo.commit_all(repo, "build + source change")
            self.assertIsNotNone(sha)
            committed = subprocess.run(["git", "show", "--name-only", "--pretty=", "HEAD"], cwd=repo,
                                        capture_output=True, text=True, check=True).stdout.split()
            self.assertIn("src.txt", committed)
            self.assertNotIn("target/classes/Demo.class", committed)

    def test_diff_stat_since_shows_changes_committed_after_base_rev(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            init_repo(repo)
            base_rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                                       text=True, check=True).stdout.strip()
            (repo / "new.txt").write_text("hello\n", encoding="utf-8")
            gitrepo.commit_all(repo, "add new.txt")
            self.assertIn("new.txt", gitrepo.diff_stat_since(repo, base_rev))

    def test_nested_module_outputs_are_excluded_without_hiding_other_builds(self):
        for tool, output in (("maven", "target"), ("gradle", "build"), ("gradle", ".gradle")):
            with self.subTest(tool=tool, output=output), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                init_repo(repo)
                build = repo / "app"
                build.mkdir()
                gitrepo.ignore_build_outputs(repo, build, tool)
                generated = build / "module" / output / "generated.txt"
                generated.parent.mkdir(parents=True)
                generated.write_text("generated")
                source = build / "module" / "Source.java"
                source.write_text("class Source {}")
                other = repo / "other" / output / "keep.txt"
                other.parent.mkdir(parents=True)
                other.write_text("unrelated")
                gitrepo.commit_all(repo, "migration")
                committed = gitrepo.capture(["git", "show", "--name-only", "--pretty=", "HEAD"], repo)
                self.assertNotIn("generated.txt", committed)
                self.assertIn("Source.java", committed)
                self.assertIn("keep.txt", committed)

    def test_diff_stat_since_head_itself_is_empty_after_a_commit(self):
        # Regression check: diffing against plain HEAD *after* committing is
        # always empty, since the working tree already matches HEAD. Callers
        # must capture base_rev before committing, not after.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            init_repo(repo)
            (repo / "new.txt").write_text("hello\n", encoding="utf-8")
            gitrepo.commit_all(repo, "add new.txt")
            self.assertEqual(gitrepo.diff_stat_since(repo, "HEAD"), "")

    def test_push_to_missing_local_destination_fails_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            init_repo(repo)
            with self.assertRaises(ModError):
                gitrepo.push(repo, "main", str(Path(tmp) / "does-not-exist"), token_env=None)


if __name__ == "__main__":
    unittest.main()
