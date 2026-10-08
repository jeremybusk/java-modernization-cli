javamod migrate \
  --source git@github.com:sqshq/piggymetrics#master \
  --verbose \
  --agent claude --agent-skill modern-java \
  --dest   git@github.com:jeremybusk/java-piggymetrics.git \
  --dest-branch modernize-java21-ai \
  --issues - \
  --report - \
  --java 21 --boot 3.5 --profile aggressive --execute
