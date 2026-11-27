# Base images are pinned by digest as well as tag: a tag is mutable, so the
# same Dockerfile could otherwise build a different (or tampered) image from one
# day to the next. Dependabot refreshes the digest — after its cooldown —
# when the tag is republished. Never drop the @sha256.
FROM docker.io/nginx:1.31.9-alpine@sha256:fb4658584bd9a916ffb6f6e365f97c25cfdb2923801f551ea08356e695f14ff7
# Remove the default page so nginx can never seed the shared static volume with
# its own root-owned index.html/50x.html (django populates it via collectstatic).
RUN rm -rf /usr/share/nginx/html/*
COPY ./compose/production/nginx/default.conf /etc/nginx/conf.d/default.conf
