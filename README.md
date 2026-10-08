# java-modernization-cli

`javamod` modernizes one Java repository and publishes the result to a
destination repo:branch. Point it at a source (remote URL or local path,
optionally at a specific ref) and a destination (remote URL or local path,
plus a branch), and it will:

1. clone/copy the source at that ref,
2. find its Maven or Gradle build and work out the current Java version,
   Spring Boot version, and framework features in use (Lombok, JUnit 4,
   Guava, `javax.*`, ...),
3. apply a plan of OpenRewrite recipes built for that target (Java version
   upgrade, Jakarta namespace, JUnit 4→5, dependency cleanup, static-analysis
   best practices), and/or an AI finishing pass,
4. reconcile formatting with the project's own enforced formatter, if it has
   one (spring-javaformat, Spotless) -- otherwise a correct migration can
   still fail validation on style alone,
5. compile (and by default test) the result,
6. commit it to a branch, and push that branch to the destination.

It is designed to need **as little per-repo configuration as possible**:
every tunable has a built-in default, every tunable can also be set with a
`JAVAMOD_*` environment variable, and a CLI flag always wins over both. Set
your team's target Java/Boot version and recipe source once in the
environment and run the same bare command against repository after
repository.

```bash
javamod doctor                 # check prerequisites
javamod migrate \
  --source git@github.com:acme/legacy-app.git \
  --dest   git@github.com:acme/modernized-app.git \
  --dest-branch modernize-java21
```

Without `--execute` this only plans, migrates, and commits to a local
checkout (printed at the end) — nothing is pushed. Add `--execute` to also
push `modernize-java21` to the destination. A build/test failure blocks the
push unless you pass `--force-push`.

## Why this exists, and what changed from the earlier tool

An earlier version of this project (`java-update-automation-tool`) grew into
a six-stage "portfolio" engine — discovery, assessment, planning, migration,
validation, publishing, each with its own approval queue, retained run
history, and GitHub/GitLab repo-auto-creation — aimed at migrating fleets of
repositories under governance. That is a reasonable thing to want, but it's
a lot of surface for the actual day-to-day job: **take this one repo, bring
it current, hand me a branch**. This rewrite keeps the useful mechanics
(recipe selection logic, the OpenRewrite invocation, the git plumbing) and
drops the fleet-governance layer, the custom portfolio file format, and the
GitHub/GitLab API client.

It also changes two defaults on purpose:

* **No Code Genome by default.** The earlier tool's `source` and `auto`
  recipe modes silently pointed at `artifacts.codegenomeproject.org`, a
  private, token-gated artifact server, even though the recipes themselves
  are open source. Here, `--recipe-source` defaults to **`maven-central`**
  (the real, publicly published `org.openrewrite.recipe:*` jars) and offers
  **`source`** (clone the matching tag of the actual
  [openrewrite](https://github.com/openrewrite) GitHub repo and
  `./gradlew publishToMavenLocal`) as a build-it-yourself alternative.
  `codegenome` still exists for sites that already pay for it, but it's
  unreachable without passing *both* `--recipe-source codegenome` **and**
  `--allow-codegenome` — no mode falls back to it silently.
* **An optional AI pass, not a replacement for OpenRewrite by default.**
  `--engine openrewrite` (the default) is deterministic recipes only — no
  API key needed, nothing to review beyond a normal diff. `--engine hybrid`
  runs the recipes and then, only if the build still fails afterward, asks
  Claude to fix the remaining compile errors (and rebuilds to check). `--engine ai`
  skips OpenRewrite entirely and asks the model to modernize each source file
  directly — useful for small repos or builds the recipes can't reach, but
  it has no build-system-level understanding of its own, so lean on
  `hybrid` for anything you'd stake a real migration on.

## Prerequisites

`javamod` itself is pure-Python stdlib. Running an actual migration needs
`git`, a JDK (`javac` included) for the target version, and Maven or Gradle
(the project's own wrapper is used automatically if present). This is built
and tested against **Ubuntu/Debian**:

```bash
./scripts/bootstrap.sh      # installs anything missing via apt-get, then
                             # creates .venv and pip-installs javamod into it
```

or open the repo in the included [devcontainer](.devcontainer/devcontainer.json)
(a Microsoft `devcontainers/java` image with Maven, Gradle, and the GitHub
CLI preinstalled) if you'd rather not touch the host at all. Either way,
`javamod doctor` tells you what's missing and how to get it.

`--engine ai` / `--engine hybrid` additionally need `pip install anthropic`
and `ANTHROPIC_API_KEY` set.

## Command reference

| Flag | Env var | Default | Meaning |
| --- | --- | --- | --- |
| `--source` | `JAVAMOD_SOURCE` | *(required)* | Git URL or local path. `source#ref` is shorthand for `--source-ref ref`. |
| `--source-ref` | `JAVAMOD_SOURCE_REF` | source's default branch / current checkout | Branch, tag, or commit to read. |
| `--dest` | `JAVAMOD_DEST` | *(required unless `--local-only`)* | Existing Git URL or local repo (ideally bare: `git init --bare`) to push to. |
| `--dest-branch` | `JAVAMOD_DEST_BRANCH` | *(required)* | Branch created/updated locally and, unless `--local-only`, at `--dest`. |
| `--build-root` | — | repo root | Path to the Maven/Gradle build, for monorepos with more than one. |
| `--java` | `JAVAMOD_JAVA` | `21` | Target Java version: 11, 17, 21, or 25. |
| `--boot` | `JAVAMOD_BOOT` | *(none)* | Target Spring Boot, e.g. `3.5` or `4.0`; only applied if Spring Boot is detected. |
| `--profile` | `JAVAMOD_PROFILE` | `standard` | `conservative` (Java upgrade only), `standard` (+ test modernization, static-analysis cleanup, build best practices), `aggressive` (+ dependency version upgrades). |
| `--dependency-strategy` | `JAVAMOD_DEPENDENCY_STRATEGY` | `patch` | `patch` or `latest`; only used when the profile upgrades dependencies. |
| `--recipe` | — | — | Extra OpenRewrite recipe name; repeatable. |
| `--recipe-source` | `JAVAMOD_RECIPE_SOURCE` | `maven-central` | `maven-central`, `source` (build from the public OpenRewrite repos), or `codegenome` (needs `--allow-codegenome`). |
| `--engine` | `JAVAMOD_ENGINE` | `openrewrite` | `openrewrite`, `hybrid` (+ AI build-fix pass), or `ai` (AI only). |
| `--ai-model` | `JAVAMOD_AI_MODEL` | `claude-sonnet-5` | Model for `hybrid`/`ai`. |
| `--skip-build` / `--skip-tests` | — | off | Skip compiling, or compile without running tests. |
| `--skip-format` | — | off | Don't run the project's own formatter (spring-javaformat/Spotless) after migrating, even if detected. |
| `--execute` | — | off (plan + local commit only) | Actually push to `--dest`. |
| `--local-only` | — | off | Commit locally; never push, even with `--execute`. |
| `--provider` | `JAVAMOD_PROVIDER` | auto-detected from the URL host | `github`/`gitlab`, selects `GH_TOKEN`/`GITLAB_TOKEN` for an HTTPS push. |
| `--workdir` | — | a temp dir | Persist the working clone here instead of deleting it. |
| `--report` | — | — | Write the JSON run report here, or `-` for stdout (CI-friendly). |
| `--quiet` | — | off | Suppress the human-readable summary; pairs with `--report -` for clean machine-readable stdout. |

Run `javamod migrate --help` for the complete, current list (it's the
source of truth; this table summarizes it).

### Crossing Spring Boot major versions

No special handling is needed. OpenRewrite's `UpgradeSpringBoot_X_Y` recipes
are fully cumulative across major versions, not just within one:
`UpgradeSpringBoot_3_0`'s own recipe list starts with `UpgradeSpringBoot_2_7`,
which in turn starts with `_2_6`, and so on down to `UpgradeSpringBoot_2_0`
("Migrate from Spring Boot 1.x to 2.0"); `UpgradeSpringBoot_4_0` likewise
starts with `UpgradeSpringBoot_3_5`. A single `--boot 3.5` run against a
Spring Boot 1.5 project reaches 3.5 in one pass -- verified directly against
[openrewrite/rewrite-spring](https://github.com/openrewrite/rewrite-spring)'s
recipe definitions and with a real migration run, not assumed.

### Using `javamod` in CI

```bash
javamod migrate --source ./app --dest-branch modernize-java21 --execute --yes \
  --report - --quiet | jq '.build_ok'
```

`--report -` writes the JSON run report to stdout instead of a file (the
usual Unix convention for "stdout" as a path); `--quiet` suppresses the
human-readable summary so stdout is clean JSON. The exit code is still `0`
on success, `1` on a build/test failure, `2` on a usage error, so a pipeline
can gate on either the exit code or a field in the JSON.

## Examples

```bash
# Plan only: see what would change, with no push and no build-tool needed
# on the destination side.
javamod migrate --source ./my-app --dest-branch modernize-java21 --local-only --skip-build -v

# A real migration to Java 21 + Spring Boot 3.5, aggressive profile,
# pushed over SSH.
javamod migrate \
  --source git@github.com:acme/legacy-app.git#main \
  --dest   git@github.com:acme/legacy-app.git \
  --dest-branch modernize-java21 \
  --issues - \
  --report - \
  --verbose \
  --java 21 --boot 3.5 --profile aggressive --execute


# Build recipes from the public OpenRewrite source repos instead of pulling
# prebuilt jars, and finish with an AI pass if the build doesn't compile.
javamod migrate --source ./my-app --dest ./my-app --dest-branch modernize-java21 \
  --recipe-source source --engine hybrid --execute

# A Spring Boot 1.5 app jumping straight to 3.5 in one pass -- OpenRewrite's
# own recipe chains back through the major-version history itself.
javamod migrate --source ./legacy-boot1-app --dest-branch modernize-java21 \
  --java 21 --boot 3.5 --execute

# Repeatable team defaults: set once, then run the bare command per repo.
export JAVAMOD_JAVA=21 JAVAMOD_PROFILE=standard JAVAMOD_RECIPE_SOURCE=maven-central
javamod migrate --source ./svc-a --dest-branch modernize-java21 --local-only
javamod migrate --source ./svc-b --dest-branch modernize-java21 --local-only
```

## Further reading

* [docs/openrewrite-overview.md](docs/openrewrite-overview.md) -- what
  OpenRewrite actually is, which recipe modules javamod uses, and a verified
  note on Moderne's Code Genome Project and whether skipping it costs you
  anything.
* [docs/residual-issue-triage.md](docs/residual-issue-triage.md) -- what
  happens when the build still fails after migration: how issues are
  classified, the YAML/JSON schema, and how to add a new verified pattern.
* [docs/formatting-reconciliation.md](docs/formatting-reconciliation.md) --
  why and how javamod re-applies a project's own formatter
  (spring-javaformat/Spotless) before validating.

## Tests

```bash
pip install -r requirements.txt
python3 -B -m unittest discover -s tests -v
```

Tests use temporary local Git repositories and mock the OpenRewrite/build/AI
calls at their module boundary — they don't need Maven, Gradle, or network
access, so they run the same in CI as on a bare checkout.
