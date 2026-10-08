# Coding-agent stage and Agent Skills

`javamod migrate --agent <cli>` adds one optional stage to a run: after the
OpenRewrite recipes, javamod hands the working clone to a coding-agent CLI
you already use and are logged in to, optionally with Agent Skills installed
for it. The agent finishes the long tail the recipes don't cover. javamod
keeps everything around it (clone, formatter, build check, triage, commit,
push), so the agent only ever edits files.

The code lives in `javamod/agent.py`.

## Where it sits in a run

```
clone source@ref
  -> detect build (Maven/Gradle, current Java/Boot, features)
  -> OpenRewrite recipes          (--engine openrewrite|hybrid)
  -> coding agent + skills        (--agent, optional)        <- this stage
  -> project's own formatter      (spring-javaformat/Spotless, if declared)
  -> javamod build check          (compile, and tests unless --skip-tests)
       \-- still failing? -> agent again with that failure, re-format, re-check
                               (up to --agent-retries times, default 0)
  -> commit -> push               (push only with --execute and a passing build)
```

The formatter runs after the agent on purpose, so style drift from the
agent's edits is cleaned up the same way as drift from the recipes.

## Why a stage inside javamod, not a separate script

A separate script would have to repeat the clone, branch setup, build
detection, build check, failure triage, commit and push that javamod already
does, or trust the agent to do them. As a stage:

* the agent's prompt is built from what javamod already detected (build
  tool, build root, current/target Java and Boot, the recipes just applied);
* javamod's own build check, not the agent's summary, decides whether the
  branch can be pushed;
* the run still ends in exactly one javamod commit on top of the source ref.

## How it differs from `--engine hybrid` / `--engine ai`

| | `--engine hybrid` / `ai` | `--agent` |
| --- | --- | --- |
| Talks to | Anthropic API directly (`ai.py`) | An agent CLI (`claude`, `codex`, `copilot`) |
| Credentials | `ANTHROPIC_API_KEY` + `pip install anthropic` | The CLI's own login/subscription |
| Behavior | Fixed prompt, full-file rewrites, javamod drives the rebuild loop | Agent explores the repo, runs the build, iterates on its own |
| Skills | No | Yes, via `--agent-skill` |

They can be combined (`--engine hybrid --agent claude`), but usually one
or the other is enough.

## Supported agents

| `--agent` | Command run | Skills installed to | Permissions |
| --- | --- | --- | --- |
| `claude` | `claude --permission-mode acceptEdits --allowedTools ... -p <prompt>` | `.claude/skills/` | Edits, plus only `mvn`/`./mvnw`/`gradle`/`./gradlew` and read-only `git status`/`git diff` in the shell |
| `codex` | `codex exec --sandbox workspace-write -c sandbox_workspace_write.network_access=true <prompt>` | `.agents/skills/` | Writes limited to the clone; network on so Maven/Gradle can resolve dependencies |
| `copilot` | `copilot --allow-all-tools -p <prompt>` | `.github/skills/` | All tools (the Copilot CLI has no narrower non-interactive mode) |
| `copilot-modernize-java` | `copilot --allow-all-tools --agent modernize-java:modernize-java -p <prompt>` | `.github/skills/` | Same as `copilot`; see the Microsoft section below |

Guardrails common to all of them:

* The CLI must be on `PATH`; javamod checks before cloning, not after the
  recipes have run.
* The prompt tells the agent never to delete, disable or weaken a test or
  its assertions, and to report failures caused by something outside the
  code (live services, network, credentials) instead of working around them.
* The prompt tells the agent not to commit, push, or switch branches. If it
  commits anyway, javamod soft-resets those commits so the changes are kept
  but folded into javamod's single commit.
* Installed skills go into the clone's `.git/info/exclude`, never into the
  project's `.gitignore`, and never into the commit.
* The transcript (command, prompt, full agent output, every pass appended
  in order) is saved and its path is shown in the summary and in the JSON
  report's `agent_log` field.
* A non-zero exit or a timeout (`--agent-timeout`, default 3600 s) is
  reported, not fatal; whatever the agent changed still goes through the
  build check.

## Retries: `--agent-retries N`

The agent already loops on its own: it runs the build, fixes, and reruns.
`--agent-retries` adds a bounded loop around that, driven by javamod's
own check instead of the agent's view of it. If javamod's build check still
fails after the agent's pass, javamod sends the agent a follow-up prompt
containing the condensed failure (the `[ERROR]` lines and Reactor Summary,
or Gradle's failed tasks/tests), re-runs the project's formatter, and checks
again. It stops at the first passing check or after `N` extra passes.

The default is `0`, because a retry only helps when the agent missed
something, such as a failure introduced by the formatter after it finished,
or one it gave up on early. When the remaining failure is outside the code,
another pass just costs time and money. And under pressure to get a passing
build, the easiest move for an agent is to weaken the test, which the prompt
forbids. Each pass is recorded: the report's `agent_passes` field, and
`agent_ok` is true only if every pass exited cleanly.

## Known-failing tests: `--skip-test`

Some tests fail for reasons that have nothing to do with the migration. In
piggymetrics, `ExchangeRatesClientTest` calls the live
`https://api.exchangeratesapi.io/latest`, which now requires an access key,
so it fails on the original code too. When you've confirmed that, exclude
the class from javamod's build check rather than letting the agent edit it:

```bash
javamod migrate ... --agent claude --skip-test ExchangeRatesClientTest
# or fully qualified: --skip-test com.piggymetrics.statistics.client.ExchangeRatesClientTest
```

Also settable as `JAVAMOD_SKIP_TESTS` (comma-separated). The decision is
yours, not the agent's. The exclusion is:

* applied without touching the project. Maven gets a temporary
  `-Dsurefire.excludesFile`, which adds to the POM's own `<excludes>`
  (`-Dtest=!X` would replace the POM's includes and excludes, quietly
  running tests the project leaves out on purpose). Gradle gets a temporary
  `--init-script` adding `Test.exclude` patterns, nested classes included;
  this works on every Gradle version, unlike `excludeTestsMatching` (5.0+).
  Both were verified against real builds.
* passed to the agent, with an instruction to leave those tests alone;
* recorded in the report (`skipped_tests`), the summary, and the commit
  message, so whoever reviews the branch sees what wasn't checked.

## Built-in skills

| `--agent-skill` | Source | License | What it adds |
| --- | --- | --- | --- |
| `modern-java` | https://github.com/brunoborges/javaevolved/tree/main/agent-plugins/modern-java-development/skills/modern-java | MIT | Version-aware guidance for idiomatic Java N, grouped by the release each feature became final in. Includes a Java-version detector script. |
| `java-version-upgrade` | https://github.com/G10xy/java-version-upgrade-skill | Apache-2.0 | What breaks between LTS versions (8→11→17→21→25) and how to fix it: removed APIs, JPMS, build-config changes, dependency compatibility. |

Each is pinned to a specific upstream commit in `BUILTIN_SKILLS`, so a run
is reproducible and an upstream change can't silently alter what the agent
is told.

Any other skill can be passed without code changes:

```bash
--agent-skill ./path/to/my-skill                              # a local directory containing SKILL.md
--agent-skill https://github.com/org/repo.git#skills/my-skill # git URL + path inside the repo
```

A git-URL skill is cloned from the default branch and is **not** pinned, so
prefer a local directory or a built-in for anything you rely on.

## Microsoft modernize-java

Source: https://github.com/microsoft/modernize-java
(the agent itself: [plugins/modernize-java](https://github.com/microsoft/modernize-java/tree/main/plugins/modernize-java))

### What it is

This is not a skill. It is a complete upgrade agent ("GitHub Copilot
modernization – Java Upgrade CLI Plugin") packaged for the GitHub Copilot
CLI. It analyzes the project, writes an upgrade plan, applies it, fixes
build and test failures until the build passes, scans for CVEs, and writes a
summary. It is made up of:

* `com.github.copilot/agents/modernize-java.agent.md` -- a Copilot-format
  agent definition (not a portable `SKILL.md`);
* `mcp.json` -- starts `@microsoft/github-copilot-app-modernization-mcp-server`
  via `npx`, a closed-source MCP server that supplies the planning,
  upgrade and CVE tools the agent depends on;
* telemetry hooks (`sendTelemetry.sh`) on prompt submit, subagent start and
  stop, and errors.

### Why it isn't a built-in here

* **It does javamod's job, not a piece of it.** Planning, applying the
  upgrade, the fix-the-build loop and the summary all overlap with what
  javamod and OpenRewrite already do. Run inside javamod, two planners
  work on the same tree; the recipes' deterministic result becomes just
  input to a second, non-deterministic upgrade.
* **Copilot CLI only.** The agent file and the plugin format are
  Copilot-specific. It can't be installed into Claude Code or Codex the way
  a portable skill can.
* **License.** Its README forbids decompiling, modifying, repackaging or
  redistributing "any assets, prompts, or internal tools", so javamod can't
  vendor, pin or copy it the way it does the built-in skills. It can only
  invoke an install you made yourself.
* **Closed, unpinned runtime pieces.** The MCP server is fetched from npm at
  run time, and the behavior comes from code you can't review.
* **Telemetry.** Its hooks send usage data to Microsoft, which a
  modernization tool that otherwise runs fully local shouldn't do by
  default.
* **Needs a GitHub Copilot subscription**, on top of whatever the rest of
  the run uses.

### Where it does fit

* **Teams already standardized on GitHub Copilot** who want Microsoft's
  supported upgrade flow and CVE check. Use it directly in the Copilot CLI
  or VS Code, without javamod:

  ```bash
  npm install -g @github/copilot
  copilot plugin marketplace add microsoft/modernize-java
  copilot plugin install modernize-java@modernize-java
  copilot --agent modernize-java:modernize-java
  ```

* **Comparing approaches.** Run it on the same repo as a plain javamod run
  and compare the two branches.
* **Through javamod, if you want both.** `--agent copilot-modernize-java`
  runs it as the agent stage after the recipes, so you keep javamod's build
  check, single commit and push handling. Install the plugin first (above).
  javamod never installs it for you.

For Claude Code or Codex, use `--agent claude` / `--agent codex` with the
portable skills above instead.

## Requirements for adding a new built-in skill

Most skills don't need to be built in; pass them as a path or git URL. Add
one to `BUILTIN_SKILLS` in `javamod/agent.py` only when it meets all of
these:

1. **Portable Agent Skill format.** A directory with a `SKILL.md` whose
   frontmatter has `name` and `description`, per the
   [Agent Skills spec](https://agentskills.io). No CLI-specific agent files,
   plugins, or MCP servers -- those go in `AGENTS` as an agent, if at all.
2. **Relevant to this job.** It helps bring an existing Java/Spring codebase
   to a newer version. General coding, style, or framework-tutorial skills
   don't belong.
3. **Open license that allows redistribution** (MIT, Apache-2.0, BSD, or
   similar), stated in the source repo. javamod copies the files into the
   clone.
4. **Reviewed and pinned.** Read the whole skill, including `references/`
   and any `scripts/`, before adding it. Pin it to a full 40-character commit
   SHA, not a branch or tag. Scripts must only read the project or run its
   build: no network calls, telemetry, or writes outside the repo.
5. **No secrets or extra services.** It must work with no API key, account,
   or service beyond the agent CLI itself.
6. **Doesn't fight the recipes.** It shouldn't tell the agent to redo what
   OpenRewrite already did (e.g. a competing upgrade plan), reformat the
   whole project, or commit/push.

To add one:

1. Add an entry to `BUILTIN_SKILLS` in `javamod/agent.py`:
   `"<short-name>": ("<git-url>.git", "<40-char-commit-sha>", "<path/to/skill/dir or ''>")`,
   with a one-line comment saying what it adds and its license.
2. Check it resolves and reports the expected name:

   ```bash
   .venv/bin/python -c "from pathlib import Path; from javamod import agent; \
     p = agent.resolve_skill('<short-name>', Path('/tmp/skillcheck')); print(agent.skill_name(p))"
   ```

3. Run the tests (`test_builtins_are_pinned_to_a_commit` checks the pin):
   `.venv/bin/python -m unittest discover -s tests`.
4. Add a row to the built-in skills table in this document and in the
   README's "Coding-agent stage" section.
5. To bump a pinned skill later, review the upstream diff between the old
   and new commit before changing the SHA.
