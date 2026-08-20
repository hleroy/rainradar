# Pinned to a full version on purpose: the bare `17` tag floats across patch
# releases, so Dependabot can only ever see a major bump (17 -> 18) and patch
# updates land invisibly on rebuild. Pinning makes each one an explicit PR.
# Majors are ignored in .github/dependabot.yml — they need pg_upgrade.
FROM docker.io/postgres:17.11

COPY ./compose/production/postgres/maintenance /usr/local/bin/maintenance
RUN chmod +x /usr/local/bin/maintenance/*
RUN mv /usr/local/bin/maintenance/* /usr/local/bin \
    && rmdir /usr/local/bin/maintenance
