javadoc migrate \
  --source git@github.com:jeremybusk/spring-boot-rest-example.git#master \
  --verbose \
  --dest   git@github.com:jeremybusk/spring-boot-rest-example.git \
  --dest-branch modernize-java21 \
  --java 21 --boot 3.5 --profile aggressive --execute
  # --source-ref master \
