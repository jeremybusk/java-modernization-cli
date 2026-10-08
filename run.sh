javamod migrate \
  --source git@github.com:jeremybusk/spring-petclinic.git#main \
  --source-ref main \
  --dest   git@github.com:jeremybusk/spring-petclinic.git \
  --dest-branch modernize-java21 \
  --java 21 --boot 3.5 --profile aggressive --execute
  # deprecated disabled --auto-stage-boot \
