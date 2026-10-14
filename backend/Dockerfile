FROM python:3.12.13-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY backend ./backend
RUN pip install --no-cache-dir uv==0.11.26 && uv sync --frozen --no-dev
COPY alembic.ini ./
COPY alembic ./alembic
ENV PATH="/app/.venv/bin:$PATH"
CMD ["sh", "-c", "alembic upgrade head && uvicorn schemashield.main:app --host 0.0.0.0 --port 8000"]
