
# define an alias for the specific python version used in this file.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS python-build-stage

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0

ARG APP_HOME=/app

WORKDIR ${APP_HOME}

# Install apt packages
RUN apt-get update && apt-get install --no-install-recommends -y \
  # dependencies for building Python packages
  build-essential \
  # psycopg dependencies
  libpq-dev


# Requirements are installed here to ensure they will be cached.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev
COPY . ${APP_HOME}

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev

# Python 'run' stage
FROM python:3.14-slim-bookworm AS python-run-stage

ARG APP_HOME=/app

WORKDIR ${APP_HOME}

RUN addgroup --system django \
  && adduser --system --ingroup django --home /home/django django


# Install required system dependencies
RUN apt-get update && apt-get install --no-install-recommends -y \
  # psycopg dependencies
  libpq-dev \
  # Translations dependencies
  gettext \
  # entrypoint
  wait-for-it \
  # cleaning up unused files
  && apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false \
  && rm -rf /var/lib/apt/lists/*


COPY --chown=django:django ./compose/production/django/entrypoint /entrypoint
RUN sed -i 's/\r$//g' /entrypoint
RUN chmod +x /entrypoint


COPY --chown=django:django ./compose/production/django/start /start
RUN sed -i 's/\r$//g' /start
RUN chmod +x /start

COPY --chown=django:django ./compose/production/django/start-archiver /start-archiver
RUN sed -i 's/\r$//g' /start-archiver
RUN chmod +x /start-archiver

# Copy the application from the builder
COPY --from=python-build-stage --chown=django:django ${APP_HOME} ${APP_HOME}
# Create the collectstatic target owned by django so the named static volume
# (mounted here in production) inherits writable ownership on first use.
RUN mkdir -p ${APP_HOME}/staticfiles && chown -R django:django ${APP_HOME}/staticfiles

# Same trick for the tile archive: pre-create /data/tiles owned by django so the
# empty `production_radar_tiles` volume inherits writable ownership when first
# mounted. The prod image runs as the non-root django user, and both this web
# container and the archiver (same image) write tiles here.
RUN mkdir -p /data/tiles && chown -R django:django /data

# make django owner of the WORKDIR directory as well.
RUN chown django:django ${APP_HOME}

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

USER django

# Collect static at build time, as the django user, so the shared static volume
# seeds from this django-owned, non-empty dir (avoids the nginx default-html
# seeding race + root-owned files that break runtime collectstatic). Dummy env
# vars satisfy production settings; collectstatic touches no DB/Redis/network.
# NB: production settings import runs the fail-fast credential guards, so every
# feature flag turned on in production.py must have its paired secret stubbed
# here too — the runtime .envs/ file does not exist during `docker build`.
# These values are never used: collectstatic neither authenticates nor connects.
RUN DJANGO_SETTINGS_MODULE=config.settings.production \
    DJANGO_SECRET_KEY=build-only \
    DJANGO_ADMIN_URL=build/ \
    DATABASE_URL=sqlite:////tmp/build.sqlite3 \
    REDIS_URL=redis://localhost:6379/0 \
    METEOFRANCE_APPLICATION_ID=build-only \
    VAPID_PUBLIC_KEY=build-only \
    VAPID_PRIVATE_KEY=build-only \
    VAPID_SUBJECT=mailto:build-only@example.invalid \
    python manage.py collectstatic --noinput

ENTRYPOINT ["/entrypoint"]
