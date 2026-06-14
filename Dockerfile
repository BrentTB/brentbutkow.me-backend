FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml constraints.txt ./
COPY app ./app
COPY scripts ./scripts
COPY alembic.ini ./
COPY alembic ./alembic
# Constraints pin the ML stack to the versions the committed model pickle was built with.
RUN pip install --upgrade pip && pip install . -c constraints.txt

# Apply migrations on start, then serve. Idempotent — `upgrade head` is a no-op when already current.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-3000}"]
