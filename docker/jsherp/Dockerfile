# Multi-stage native ARM64/AMD64 build for official jishenghua/jshERP (jshERP-boot)
FROM maven:3.8-openjdk-8 AS builder

ARG JSHERP_REPO=https://github.com/jishenghua/jshERP.git
ARG JSHERP_REF=master

RUN mkdir -p /root/.m2 && cat > /root/.m2/settings.xml <<'EOF'
<settings xmlns="http://maven.apache.org/POM/4.0.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/settings-1.0.0.xsd">
  <mirrors>
    <mirror>
      <id>aliyunmaven</id>
      <mirrorOf>central</mirrorOf>
      <name>Aliyun Central</name>
      <url>https://maven.aliyun.com/repository/public</url>
    </mirror>
  </mirrors>
</settings>
EOF

RUN git clone --depth 1 --branch "${JSHERP_REF}" "${JSHERP_REPO}" /src/jshERP

WORKDIR /src/jshERP/jshERP-boot
RUN mvn -B -DskipTests -Dmaven.test.skip=true -Dmaven.compiler.skip=false -Dmaven.assembly.skip=true clean package \
    && test -f target/jshERP.jar

FROM eclipse-temurin:8-jdk

RUN apt-get update \
    && apt-get install -y --no-install-recommends default-mysql-client curl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/jshERP/upload /opt/jshERP/export /opt/tmp/tomcat

COPY --from=builder /src/jshERP/jshERP-boot/target/jshERP.jar /opt/jshERP/jshERP.jar
COPY --from=builder /src/jshERP/jshERP-boot/docs/jsh_erp.sql /opt/jshERP/jsh_erp.sql
COPY entrypoint.sh /opt/jshERP/entrypoint.sh
RUN chmod +x /opt/jshERP/entrypoint.sh

ENV SERVER_PORT=9999 \
    DB_HOST=jsherp-mysql \
    DB_PORT=3306 \
    DB_NAME=jsh_erp \
    DB_USER=root \
    DB_PASS=123456 \
    REDIS_HOST=jsherp-redis \
    REDIS_PORT=6379 \
    REDIS_PASS=1234abcd \
    DISABLE_LOGIN_CAPTCHA=1 \
    JAVA_OPTS="-Xms256m -Xmx512m"

EXPOSE 9999

ENTRYPOINT ["/opt/jshERP/entrypoint.sh"]
