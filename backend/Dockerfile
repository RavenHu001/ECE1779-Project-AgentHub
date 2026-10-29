# Share one non-root runtime image between API, worker and migration commands.
# API、worker 和迁移命令共用非 root 运行镜像。
FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC
WORKDIR /app
COPY requirements.txt ./
RUN pip install --requirement requirements.txt \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Keep test dependencies and writable caches out of the runtime image.
# 测试依赖及可写缓存只放在测试镜像中。
FROM runtime AS test
ENV RUFF_CACHE_DIR=/tmp/ruff-cache \
    PYTEST_ADDOPTS="-p no:cacheprovider"
USER root
COPY requirements-dev.txt ./
RUN pip install --requirement requirements-dev.txt
COPY pyproject.toml ./
COPY tests ./tests
USER app
CMD ["pytest", "-q"]
