# Base images are pinned by digest as well as tag: a tag is mutable, so the
# same Dockerfile could otherwise build a different (or tampered) image from one
# day to the next. Dependabot refreshes the digest — after its cooldown —
# when the tag is republished. Never drop the @sha256.
FROM docker.io/nginx:1.31.7-alpine@sha256:892bb2152464f0c5d8972593a5f5afaf8054552580b699d95f8ff4af08c2d33d
# Remove the default page so nginx can never seed the shared static volume with
# its own root-owned index.html/50x.html (django populates it via collectstatic).
RUN rm -rf /usr/share/nginx/html/*
COPY ./compose/production/nginx/default.conf /etc/nginx/conf.d/default.conf
