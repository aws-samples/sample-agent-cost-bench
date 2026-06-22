# kirobench Java verification image — Amazon Corretto 17 + Maven + warmed ~/.m2.
# Base: Amazon Corretto JDK 17 from the Amazon ECR Public Gallery (no Docker
# Hub rate limits). Maven is installed on top from the Amazon Linux package repo.
FROM public.ecr.aws/amazoncorretto/amazoncorretto:17-al2023

# Install Maven (Amazon Linux 2023 ships maven 3.x in its default repos).
RUN dnf install -y maven && dnf clean all

# Warm the local Maven repo with JUnit + plugins + the Surefire test provider.
WORKDIR /warm
COPY warm-pom.xml ./pom.xml
RUN mkdir -p src/test/java/warm && \
    printf 'package warm;\nimport org.junit.jupiter.api.Test;\nclass WarmTest { @Test void ok() {} }\n' \
      > src/test/java/warm/WarmTest.java && \
    mvn -B -q test

WORKDIR /work
