FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

USER app

ENTRYPOINT ["frigate-media-backup"]
CMD ["--config", "/config/config.yaml"]

