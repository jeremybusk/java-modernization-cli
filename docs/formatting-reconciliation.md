# Formatting reconciliation

Some projects bind a formatter's "check" goal into their own build --
most commonly [spring-javaformat](https://github.com/spring-io/spring-javaformat)
or [Spotless](https://github.com/diffplug/spotless). Any tool that rewrites
source, OpenRewrite included, produces a tree that compiles and behaves
correctly but can still fail that check on formatting alone: import order,
line wrapping, whitespace -- nothing functional, but a hard build failure
all the same.

`javamod/formatting.py` detects which formatter (if any) a project declared
itself, and runs its "apply" goal right after the OpenRewrite/AI steps and
before the build is validated -- the same thing a developer would do by
hand after a large automated change.

## Detection

Maven: scans `pom.xml` for a `<plugin>` with artifactId
`spring-javaformat-maven-plugin` or `spotless-maven-plugin`.

Gradle: scans `build.gradle`/`build.gradle.kts` for the plugin id
`io.spring.javaformat` or `com.diffplug.spotless`.

No formatter declared -> no-op, nothing runs.

## The goals/tasks run

Verified against each project's own README, not assumed:

| Build tool | Formatter | Goal/task |
| --- | --- | --- |
| Maven | spring-javaformat | `spring-javaformat:apply` |
| Maven | Spotless | `spotless:apply` |
| Gradle | spring-javaformat | `format` |
| Gradle | Spotless | `spotlessApply` |

The command runs through the **project's own** `mvnw`/`gradlew` wrapper
(falling back to a system `mvn`/`gradle` only if no wrapper is present) --
unlike the OpenRewrite invocation itself, which deliberately uses the
system Maven/Gradle regardless of the project's wrapper (see the README's
"Why this exists" section). The formatter plugin's version is declared in
the project's own build file, so the project's own build tool is what
correctly resolves it; there's no coordinate for `javamod` to pass itself.

## Turning it off

`--skip-format`. Useful mainly for debugging whether a build failure is
really a formatting disagreement or something else -- compare the build
output with and without this flag.

## A real example

Running `javamod migrate --boot 3.5 --java 21` against
[spring-petclinic](https://github.com/spring-projects/spring-petclinic)'s
`main` branch failed validation with 36 files worth of
`spring-javaformat-maven-plugin` violations and `Run 'spring-javaformat:apply'
to fix.` in the output -- the migration itself was correct (OpenRewrite had
already left the Spring Boot parent version untouched, since the project was
already past the requested target; see
[residual-issue-triage.md](residual-issue-triage.md) for how a genuine
leftover failure is reported). Running the suggested goal turned that into
a clean `BUILD SUCCESS`, 81/81 tests passing -- confirming this exact gap
before it became a permanent part of `javamod`.
