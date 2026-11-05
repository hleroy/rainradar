# Base images are pinned by digest as well as tag: a tag is mutable, so the
# same Dockerfile could otherwise build a different (or tampered) image from one
# day to the next. Dependabot refreshes the digest — after its cooldown —
# when the tag is republished. Never drop the @sha256.
FROM docker.io/nginx:1.31.8-alpine@sha256:785835ea65db4bba6f021acc0f3be91b0fef76a53fe41f8588c6698d2aed0606
# Remove the default page so nginx can never seed the shared static volume with
# its own root-owned index.html/50x.html (django populates it via collectstatic).
RUN rm -rf /usr/share/nginx/html/*
COPY ./compose/production/nginx/default.conf /etc/nginx/conf.d/default.conf
