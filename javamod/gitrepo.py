"""Git plumbing: pull a source repo:ref in, push a result out to repo:branch.

Everything here is deliberately plain ``git`` subprocess calls -- no
GitHub/GitLab API clients, no auto-created repositories, no draft PRs. The
destination is expected to already exist (a bare repo is simplest); javamod's
job is to land one commit on one branch there, not to administer hosting.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from .errors import ModError

_REMOTE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://|^[\w.-]+@[\w.-]+:")


def is_remote(location: str) -> bool:
    """True for anything git would treat as a URL (scheme:// or user@host:path)."""
    return bool(_REMOTE_RE.match(location))


def split_ref(location: str) -> tuple[str, str | None]:
    """Split an optional trailing ``#ref`` off *location*.

    Git URLs and filesystem paths don't otherwise use ``#``, so
    ``git@host:org/app.git#release/9`` and ``./local-checkout#main`` are
    unambiguous. Prefer the explicit ``--source-ref`` flag when a path or URL
    genuinely contains a ``#``.
    """
    if "#" in location:
        base, ref = location.rsplit("#", 1)
        if base and ref:
            return base, ref
    return location, None


def run(cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None,
        timeout: int = 900, display: str | None = None) -> str:
    try:
        completed = subprocess.run(
            cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise ModError(f"command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ModError(f"timed out after {timeout}s: {display or shlex.join(cmd)}") from exc
    if completed.returncode != 0:
        shown = display or shlex.join(cmd)
        raise ModError(f"command failed ({completed.returncode}): {shown}\n{completed.stdout[-4000:]}")
    return completed.stdout


def capture(cmd: list[str], cwd: Path) -> str:
    return run(cmd, cwd).strip()


def make_askpass(workdir: Path, token_env: str, username: str = "x-access-token") -> Path:
    """A tiny GIT_ASKPASS helper so an HTTPS token never appears in argv or logs."""
    path = workdir / ".git-askpass.sh"
    path.write_text(
        "#!/bin/sh\ncase \"$1\" in\n"
        f"*sername*) printf '%s\\n' \"{username}\" ;;\n"
        f"*) printf '%s\\n' \"${{{token_env}:-}}\" ;;\nesac\n",
        encoding="utf-8",
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


def git_env(workdir: Path, token_env: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token_env and os.environ.get(token_env):
        env["GIT_ASKPASS"] = str(make_askpass(workdir, token_env))
    return env


def clone_source(source: str, ref: str | None, workdir: Path, *, allow_dirty: bool,
                  shallow: bool, token_env: str | None) -> Path:
    """Materialize *source* at *ref* under ``workdir/src`` and return that path."""
    dest = workdir / "src"
    if is_remote(source):
        env = git_env(workdir, token_env)
        cmd = ["git", "clone", "--no-tags"]
        if shallow:
            cmd += ["--depth", "1"]
        cmd += [source, str(dest)]
        run(cmd, cwd=workdir, env=env, display=shlex.join(cmd[:-2] + ["<source>", str(dest)]))
        if ref:
            run(["git", "fetch", *(["--depth", "1"] if shallow else []), "origin", ref],
                cwd=dest, env=env)
            run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=dest, env=env)
        return dest
    local = Path(source).expanduser().resolve()
    if not local.is_dir():
        raise ModError(f"source directory does not exist: {local}")
    if (local / ".git").exists():
        if capture(["git", "status", "--porcelain"], local) and not allow_dirty:
            raise ModError(f"source has uncommitted changes (pass --allow-dirty): {local}")
        run(["git", "clone", "--no-hardlinks", "--no-tags", str(local), str(dest)], cwd=workdir)
        if ref:
            # Resolve in the source: --no-tags intentionally omits local tag
            # names, and a tag may point at a commit absent from any branch.
            revision = capture(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], local)
            run(["git", "fetch", "--no-tags", "origin", revision], cwd=dest)
            run(["git", "checkout", "--detach", revision], cwd=dest)
        return dest
    # A plain (non-git) directory: copy it so the source tree is untouched.
    if ref:
        raise ModError("a source ref requires a Git repository; the plain directory has none")
    if workdir.resolve().is_relative_to(local):
        raise ModError("--workdir must be outside a plain source directory to avoid copying the checkout into itself")
    shutil.copytree(local, dest, symlinks=True,
                     ignore=shutil.ignore_patterns(".git", "target", "build", ".gradle", "node_modules"))
    run(["git", "init", "-q", "-b", "main"], cwd=dest)
    run(["git", "add", "-A"], cwd=dest)
    run(["git", "-c", "user.email=javamod@local", "-c", "user.name=javamod",
         "commit", "-q", "-m", "Snapshot of ungoverned source directory"], cwd=dest)
    return dest


def current_branch(repo: Path) -> str:
    return capture(["git", "branch", "--show-current"], repo)


def ensure_branch(repo: Path, branch: str) -> None:
    # This is an isolated clone: name the selected source revision even if
    # the source already has a branch with the destination's name.
    run(["git", "switch", "-C", branch], cwd=repo)


def diff_stat_since(repo: Path, base_rev: str) -> str:
    """``git diff --stat`` of everything committed since *base_rev*.

    Call this against a rev captured *before* any commits were made --
    comparing to plain ``HEAD`` after a commit just diffs the working tree
    against itself and is always empty.
    """
    return capture(["git", "diff", "--stat", base_rev, "HEAD"], repo) if _has_commits(repo) else ""


def changed_files(repo: Path) -> list[str]:
    out = capture(["git", "status", "--porcelain"], repo)
    return [line[3:] for line in out.splitlines() if line]


def _has_commits(repo: Path) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=repo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def commit_all(repo: Path, message: str) -> str | None:
    """Stage and commit everything; return the new commit sha, or None if nothing changed."""
    run(["git", "add", "-A"], cwd=repo)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo).returncode == 0:
        return None
    run(["git", "-c", "user.email=javamod@local", "-c", "user.name=javamod",
         "commit", "-q", "-m", message], cwd=repo)
    return capture(["git", "rev-parse", "HEAD"], repo)


def ignore_build_outputs(repo: Path, build_root: Path, tool: str) -> None:
    """Keep Maven/Gradle output out of the commit without editing the
    project's own .gitignore. The build has to actually run (for validation)
    before the commit, so this must be set up first -- otherwise ``git add
    -A`` happily stages ``target/``/``build/`` right along with the source
    changes.
    """
    relative = build_root.resolve().relative_to(repo.resolve()).as_posix()
    prefix = "" if relative == "." else relative + "/"
    names = ("target",) if tool == "maven" else ("build", ".gradle")
    exclude(repo, [f"/{prefix}**/{name}/" for name in names])


def exclude(repo: Path, patterns: list[str]) -> None:
    """Append *patterns* to the clone's own .git/info/exclude, never its .gitignore."""
    path = Path(capture(["git", "rev-parse", "--git-path", "info/exclude"], repo))
    if not path.is_absolute():
        path = repo / path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n" + "\n".join(patterns) + "\n")


def init_bare_destination(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q", "--bare", str(path)], cwd=path)


def push(repo: Path, branch: str, destination: str, *, token_env: str | None,
         force: bool = False) -> None:
    """Push the current HEAD of *repo* to ``branch`` on *destination*."""
    if not is_remote(destination):
        local = Path(destination).expanduser().resolve()
        if not (local / "HEAD").exists() and not (local / ".git").exists():
            raise ModError(
                f"destination does not exist or is not a git repository: {local}\n"
                "Create it first, e.g. `git init --bare` (or pass --init-dest)."
            )
        destination = str(local)
        env = dict(os.environ)
        scratch = None
    else:
        # A scratch dir outside the repo -- the askpass helper must not land
        # inside the working tree javamod just finished committing.
        scratch = tempfile.mkdtemp(prefix="javamod-askpass-")
        env = git_env(Path(scratch), token_env)
    try:
        refspec = f"HEAD:refs/heads/{branch}"
        cmd = ["git", "push"] + (["--force"] if force else []) + [destination, refspec]
        display = shlex.join(cmd[:-2] + ["<destination>", refspec])
        run(cmd, cwd=repo, env=env, display=display)
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
