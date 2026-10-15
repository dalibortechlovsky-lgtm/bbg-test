FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ARG APP_RELEASE=development
ENV APP_RELEASE=$APP_RELEASE
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir /data && chown app:app /data
COPY --chown=app:app src /app/src
COPY --chown=app:app web /app/web
COPY --chown=app:app bridge /app/bridge
USER app
EXPOSE 8000
CMD ["python", "-m", "src.server", "--host", "0.0.0.0", "--db", "/data/frontoffice.sqlite3"]
