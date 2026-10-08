#!/usr/bin/env bash
javamod migrate \
  --source git@github.com:sqshq/piggymetrics#master \
  --verbose \
  --dest   git@github.com:jeremybusk/java-piggymetrics.git \
  --dest-branch modernize-java21 \
  --issues - \
  --report - \
  --java 21 --boot 3.5 --profile aggressive --execute
