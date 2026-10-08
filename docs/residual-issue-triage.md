# Residual issue triage

OpenRewrite's recipes do the deterministic, mechanical part of a migration:
version bumps, namespace renames, dependency coordinate changes, test
framework migration, static-analysis cleanup. What's left after a build
still fails is -- by definition -- the part that needed a judgment call: a
dependency with no automated migration path (SpringFox has no Spring Boot 3
support at all), an API removed with no drop-in replacement, or something
nobody involved in this project has hit before.

`javamod/triage.py` handles the deterministic part of *that* too: it parses
the failed build's raw output into distinct, located problems, and attaches
a known remediation hint only where one has actually been confirmed against
a real build -- never a guess. An issue that doesn't match a known pattern
is still reported, with its real facts (file, line, compiler message) and
`null` fix fields, so whoever picks it up next -- a person, or an AI agent --
has evidence to reason from instead of invented advice.

## When it runs

Automatically, whenever `javamod migrate` validates the build (i.e.
`--skip-build` wasn't passed) and that validation still fails after the
OpenRewrite pass, the optional AI engine, and formatting reconciliation
(see [formatting-reconciliation.md](formatting-reconciliation.md)) have all
had their turn. There's nothing to configure to turn it on.

## Where it shows up

1. **The terminal, immediately.** Printed as soon as the build fails, not
   gated behind `-v` -- this is a result, not debug noise. Suppressed only
   by `--quiet` (which is meant to leave stdout as clean JSON for a
   pipeline; see the `--report` field below instead).
2. **A YAML file.** `--issues PATH` (default: `<workdir>/remaining-issues.yaml`,
   next to the kept, failed checkout). Pass `--issues -` to write it to
   stdout instead, the same convention `--report -` uses.
3. **The JSON run report**, as a `residual_issues` field, whenever `--report`
   is used -- so a CI pipeline gets the same structured data without
   scraping terminal output or a second file.

## The schema

Each issue is one record:

```yaml
issues:
  - file: "src/main/java/com/example/api/docs/SwaggerConfig.java"
    lines: [9, 10, 11, 12]
    category: "unresolved-dependency"
    message: "package springfox.documentation.builders does not exist"
    likely_cause: "springfox-swagger2 has no Spring Boot 3+/Jakarta support and is unmaintained."
    recommended_fix: "Replace the springfox-swagger2 dependency with springdoc-openapi (e.g. org.springdoc:springdoc-openapi-starter-webmvc-ui) and rewrite the Docket-based config to springdoc's OpenAPI bean/annotation API."
    confidence: "high"
```

| Field | Meaning |
| --- | --- |
| `file` | The source file the compiler pointed at, or `TestClass.method` for a failing test. |
| `lines` | Every line number reported for this issue, deduplicated. |
| `category` | `unresolved-dependency`, `removed-api`, `renamed-api`, `changed-signature`, `missing-dependency-version`, `test-failure` (a failing test with no matched pattern), or `unknown`. |
| `message` | The compiler's own message (or test failure detail), as reported. |
| `likely_cause` / `recommended_fix` | Set only when a known pattern matched; `null` otherwise. Never fabricated. |
| `confidence` | `high` for a matched, verified pattern; `unverified` otherwise. |

Multiple raw compiler lines that are really the same root problem (e.g. five
different "cannot find symbol" errors in one file, all because one import
is missing) are grouped into one issue by file + matched pattern (or, for
unmatched issues, file + the first 80 characters of the message) rather than
reported as five unrelated entries. Maven also prints every compile error
twice -- once inline, once again in its goal-failure summary -- which is
deduplicated by (file, line) before grouping, keeping whichever copy carried
more context.

Failing tests are parsed from Surefire 3's end-of-module summary
(`[ERROR] Failures:` / `[ERROR] Errors:` blocks; flaky-test `Run N:` lines
are skipped), from the older Surefire `Tests in error:` block, and from
Gradle's `Class > method FAILED` lines. Each failing test method is its own
issue, with the line the runner reported. triage can't tell from the output
alone *why* a test fails -- a live external service, for example, looks like
any other assertion failure -- so these get no fix text. If you've confirmed
a test fails for reasons outside the migration, exclude it with
`--skip-test` (see [agent-stage-and-skills.md](agent-stage-and-skills.md#known-failing-tests---skip-test)).

## The known-pattern table

Deliberately short, in `javamod/triage.py`'s `KNOWN_PATTERNS`. Every entry
was hit and confirmed against a real failing build in this project's own
history -- not invented from general Spring/Java knowledge:

| Pattern | Category | Signature(s) matched |
| --- | --- | --- |
| `springfox-unmaintained` | `unresolved-dependency` | `"springfox"` |
| `actuator-metrics-removed` | `removed-api` | `"CounterService"`, `"GaugeService"` |
| `spring-data-findone-removed` | `renamed-api` | `"findOne("` |
| `pagerequest-two-arg-constructor-removed` | `changed-signature` | `"PageRequest cannot be applied"` |

### Adding a new pattern

Only add an entry once you've actually seen the error and confirmed the fix
-- against a real build, not from memory of how a library "should" behave.
The whole value of this feature is that `recommended_fix` can be trusted
at face value; a plausible-sounding but unverified guess is worse than the
honest `null` an unmatched issue gets. To add one:

1. Reproduce the failure with `javamod migrate ... --skip-format` (or
   without, if formatting isn't the issue) and capture the real compiler
   output.
2. Add an entry to `KNOWN_PATTERNS` in `javamod/triage.py`: a short `name`,
   one or more literal `signatures` (substrings that appear in the actual
   compiler message), a `category`, and `likely_cause`/`recommended_fix`
   text you've verified actually resolves it.
3. Add a test in `tests/test_triage.py` using the real (or faithfully
   reproduced) compiler output as the fixture, asserting the new pattern is
   matched with the right fix text.

## Relationship to `--engine hybrid`

`--engine hybrid` (see the main [README](../README.md#command-reference))
is the other side of this: given `ANTHROPIC_API_KEY`, it feeds a failing
build's compiler output and the implicated files to Claude and retries,
without a human in the loop. Triage is deliberately independent of that --
it needs no API key, runs every time, and is exactly the artifact you'd
want to hand an AI agent (this one or any other) if you *do* decide to wire
one up: a short, structured, evidence-backed list of exactly what's left
and why, instead of a wall of raw compiler output.
