javamod migrate \
  --source git@github.com:jeremybusk/spring-petclinic.git#main \
  --verbose \
  --dest   git@github.com:jeremybusk/spring-petclinic.git \
  --dest-branch modernize-java21 \
  --java 21 --boot 3.5 --profile aggressive --execute
  # --source-ref main \
