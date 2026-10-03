# Pinned to a full version on purpose: the bare `17` tag floats across patch
# releases, so Dependabot can only ever see a major bump (17 -> 18) and patch
# updates land invisibly on rebuild. Pinning makes each one an explicit PR.
# Majors are ignored in .github/dependabot.yml — they need pg_upgrade.
# Also pinned by digest: tags are mutable; Dependabot refreshes the digest
# (after its cooldown) when the tag is republished. Never drop the @sha256.
FROM docker.io/postgres:17.11@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f

COPY ./compose/production/postgres/maintenance /usr/local/bin/maintenance
RUN chmod +x /usr/local/bin/maintenance/*
RUN mv /usr/local/bin/maintenance/* /usr/local/bin \
    && rmdir /usr/local/bin/maintenance
