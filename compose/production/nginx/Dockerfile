FROM docker.io/nginx:1.31.3-alpine
# Remove the default page so nginx can never seed the shared static volume with
# its own root-owned index.html/50x.html (django populates it via collectstatic).
RUN rm -rf /usr/share/nginx/html/*
COPY ./compose/production/nginx/default.conf /etc/nginx/conf.d/default.conf
