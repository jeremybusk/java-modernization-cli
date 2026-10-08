# OpenRewrite, briefly

[OpenRewrite](https://docs.openrewrite.org/) is an open-source (Apache 2.0)
refactoring engine for Java (and other JVM-ish ecosystems). It parses source
into a lossless semantic tree -- not just text -- applies a *recipe*
(itself often a composed list of smaller recipes) to that tree, and writes
back source that preserves everything a text-based find/replace would
mangle: comments, formatting, import ordering. Recipes are typically type-
aware, so "rename this method across the whole project" actually resolves
which `foo()` you meant rather than matching the string everywhere.

A recipe is distributed as a small library (e.g.
`org.openrewrite.recipe:rewrite-migrate-java`) containing recipe
definitions plus any supporting Java code. `javamod` runs them by invoking
the real `rewrite-maven-plugin`/Gradle plugin directly against a project's
own build -- see [the main README](../README.md#why-this-exists-and-what-changed-from-the-earlier-tool)
for why that invocation deliberately uses the system Maven/Gradle rather
than the project's own wrapper.

The recipe modules `javamod` currently uses:

| Module | What it covers |
| --- | --- |
| `rewrite-migrate-java` | Java version upgrades, deprecated-for-removal JDK API cleanup, `javax`→`jakarta`. |
| `rewrite-spring` | Spring Boot version upgrades (see [recipes.py](../javamod/recipes.py)'s `spring_boot_recipe`). |
| `rewrite-testing-frameworks` | JUnit 4→5 migration and cleanup. |
| `rewrite-static-analysis` | General code cleanup (diamond operators, redundant casts, etc). |
| `rewrite-java-dependencies` | Dependency version bumps (`--profile aggressive` only). |

## Moderne and the Code Genome Project

OpenRewrite the project is independent, but [Moderne](https://moderne.io)
(the company several of its original authors founded) publishes most of
the maintained recipe modules, including all five above. In 2026 Moderne
introduced the
[Code Genome Project](https://docs.moderne.io/user-documentation/recipes/accessing-the-code-genome-project/),
its own Maven-compatible artifact repository, citing new publishing limits
Sonatype placed on Maven Central that didn't fit OpenRewrite's release
cadence and artifact sizes. It has three access tiers:

1. **Open-source recipes** (Apache 2.0), the CLI, and the Connector --
   available to anyone with a (free) Moderne account and download token.
2. **MSAL** (Moderne Source Available License) recipes -- customers only.
3. **Proprietary recipes** -- customers only.

### Does `javamod` lose much by not using it?

**For what `javamod` actually uses: not much, and nothing you can't get
back for free.** Every module in the table above is tier 1 -- open source,
Apache 2.0, source available on GitHub regardless of Code Genome. Moderne
not publishing new *releases* to Maven Central doesn't mean the *source*
stopped being public.

What you concretely lose by never touching Code Genome is **currency on
Maven Central specifically** -- and this is measurable, not theoretical.
Checked directly against both repositories:

| Module | Latest on GitHub | Latest on Maven Central | Gap |
| --- | --- | --- | --- |
| `rewrite-spring` | `v6.40.0` | `6.37.1` (frozen since 2026-08-19) | 3 releases |
| `rewrite-migrate-java` | `v3.45.0` | `3.42.1` (frozen since 2026-08-19) | 3 releases |

Maven Central's `maven-metadata.xml` for both stopped updating on the same
day, which lines up with Moderne's own account of why Code Genome exists:
newer releases are going there, not to Central.

This is exactly why `javamod` ships a third option beyond "use what's on
Central" or "get a Moderne account": **`--recipe-source source`** clones
the matching tag straight from the actual public GitHub repository (e.g.
`github.com/openrewrite/rewrite-spring`) and builds it locally with
`./gradlew publishToMavenLocal`. That gets you the current release with no
Moderne account, no token, and no cost -- just a few extra minutes per
module, cached after the first build (see
[`javamod/openrewrite.py`](../javamod/openrewrite.py)'s
`ensure_source_recipes_built`).

`--recipe-source codegenome` still exists and is never reached unless you
pass *both* `--recipe-source codegenome` and the explicit `--allow-codegenome`
flag (see the main README) -- for sites that already have a Moderne
account, want the convenience of a prebuilt binary over a local source
build, or need an MSAL/proprietary recipe this project doesn't currently
use. For everyone else, `source` mode is the way to stay current without
an account at all, and `maven-central` (the default) remains the simplest,
zero-build option as long as being a handful of releases behind doesn't
matter for the migration at hand.
