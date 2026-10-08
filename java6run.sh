javamod migrate \
  --source git@github.com:jeremybusk/spring-boot-rest-example.git#main \
  --source-ref master \
  --dest   git@github.com:jeremybusk/spring-boot-rest-example.git \
  --dest-branch modernize-java21 \
  --java 21 --boot 3.5 --profile aggressive --execute
