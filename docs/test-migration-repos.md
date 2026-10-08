A curated list of GitHub repositories ideal for testing automated OpenRewrite migration recipes (such as `MigrateToJava21`, `UpgradeSpringBoot_3_2`, `JUnit4to5Migration`, and dependency cleanup), categorized by architecture and legacy stack:

| Repository / Tag | Build Tool | Starting Baseline | Key Migration Challenges Tested |
| --- | --- | --- | --- |
| [openrewrite/spring-petclinic-migration](https://github.com/openrewrite/spring-petclinic-migration) | Maven | Java 8, Spring Boot 1.5.x | Official OpenRewrite reference repo; tests multi-hop Boot upgrades (1.5 -> 2.x -> 3.x), `javax.*` to `jakarta.*`, and JUnit 4 to 5. |
| [spring-projects/spring-petclinic](https://github.com/spring-projects/spring-petclinic) (`v2.1.0.RELEASE` or `v1.5.4.RELEASE`) | Maven | Java 8, Spring Boot 2.1 / 1.5 | Clean standard architecture; ideal baseline to verify recipe chaining without breaking Spring Data JPA or Thymeleaf templates. |
| [spring-petclinic/spring-petclinic-rest](https://github.com/spring-petclinic/spring-petclinic-rest) (`v2.2.5`) | Maven & Gradle | Java 8, Spring Boot 2.2 | Supports dual build tools; verifies recipe execution on both Maven POMs and Gradle Groovy scripts, Jackson date/time rewrites, and OpenAPI/Swagger changes. |
| [sqshq/piggymetrics](https://github.com/sqshq/piggymetrics) | Maven | Java 8, Spring Boot 1.5, Spring Cloud (Dalston) | Complex multi-module microservice architecture; tests legacy Spring Cloud Netflix components (Eureka, Zuul, Hystrix deprecations), OAuth2 migrations, and sub-module POM rewriting. |
| [cargotracker/cargotracker](https://www.google.com/search?q=https://github.com/cargotracker/cargotracker) | Maven | Java 8 / Java EE 8 | Enterprise Jakarta EE / Java EE baseline; tests Jakarta namespace migrations (`javax.persistence`, `javax.ejb`, `javax.inject`), CDI upgrades, and non-Spring dependency trees. |
| [eugenp/tutorials](https://github.com/eugenp/tutorials) (target specific submodules like `/spring-boot-modules/spring-boot-legacy` or `/core-java-modules/core-java-8`) | Maven | Java 8 | Hundreds of isolated, legacy JDK 8 test benches; tests atomic Java 8 idioms (`Optional`, lambdas, Date/Time API, Streams) upgrading to modern constructs. |
| [openrewrite/rewrite-testing-frameworks](https://github.com/openrewrite/rewrite-testing-frameworks) | Gradle | Java 8 / 11 | Source test fixtures for test migrations; checks edge cases in JUnit 4 `@Rule`, Hamcrest matchers, and AssertJ conversion. |
| [moderneinc/rewrite-recipe-starter](https://github.com/moderneinc/rewrite-recipe-starter) | Gradle | Java 17/21 baseline | Useful harness for testing custom local Java visitors and imperative recipes against mock ASTs before applying them across target repositories. |

### Verification Checklist

1. **Pin Git Tags:** Always check out explicit legacy tags or commit SHAs rather than `main`, as upstream repositories frequently merge automated upgrade PRs.
2. **JDK Toolchain Alignment:** Execute `rewrite:run` using JDK 17 or JDK 21 while setting `--add-exports` flags if running OpenRewrite directly on older source compatibility trees.
3. **Multi-Hop Chaining:** For repos on Spring Boot 1.5 (like PiggyMetrics or legacy PetClinic), run the `org.openrewrite.java.spring.boot2.SpringBoot2JUnit4to5Migration` chain before jumping directly into Spring Boot 3 / Jakarta EE namespaces.
