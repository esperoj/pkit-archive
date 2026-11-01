FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml LICENSE ./
COPY pkit ./pkit

RUN pip install --no-cache-dir .

RUN groupadd --gid 1000 pkit \
    && useradd --uid 1000 --gid pkit --create-home pkit

USER pkit

ENTRYPOINT ["pkit"]
CMD ["--help"]
