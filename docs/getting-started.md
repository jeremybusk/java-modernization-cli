# Getting started

These commands use Bash on Linux or macOS. You need Python 3.11+, Git,
a JDK for your target Java version (21 by default), and system Maven or
Gradle for your project's build tool.

1. Clone the repository:

   ```bash
   git clone https://github.com/jeremybusk/java-modernization-cli.git
   ```

2. Enter the repository:

   ```bash
   cd java-modernization-cli
   ```

3. Create a Python virtual environment:

   ```bash
   python3 -m venv .venv
   ```

4. Activate it:

   ```bash
   source .venv/bin/activate
   ```

5. Install the `javamod` command:

   ```bash
   python -m pip install -e .
   ```

6. Check the CLI and prerequisites:

   ```bash
   javamod --help
   javamod doctor
   ```

   On Ubuntu/Debian, `./scripts/bootstrap.sh` can install missing system
   prerequisites and set up the environment. AI packages and agent CLIs
   reported as missing are optional for the default OpenRewrite engine.

7. Run a local migration, replacing `../my-java-app` with your Java project:

   ```bash
   javamod migrate --source ../my-java-app --dest-branch modernize-java21 \
     --java 21 --local-only
   ```

   The migrated checkout is retained locally, and its path is printed.
   If both Maven and Gradle build files exist, add `--build-tool gradle`
   or `--build-tool maven` to select the one your project uses. Alternate
   build files are reported for review and retained.
   See the [README](../README.md#command-reference) for Spring Boot targets,
   agents, and publishing the result.

In each new terminal, run `source .venv/bin/activate` from this repository,
or invoke `.venv/bin/javamod` directly.

For the optional `--engine ai` or `--engine hybrid`, install the AI extra
and set your Anthropic API key:

```bash
python -m pip install -e '.[ai]'
export ANTHROPIC_API_KEY="your-api-key"
```
