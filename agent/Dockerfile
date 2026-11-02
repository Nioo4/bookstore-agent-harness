FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app/agent
WORKDIR /app/agent
COPY agent/pyproject.toml ./
COPY agent/requirements.lock ./
RUN pip install -r requirements.lock && useradd --uid 10001 --create-home bookstore
COPY agent/app/ ./app/
COPY agent/rpc/ ./rpc/
RUN pip install --no-deps .
COPY agent/config/ ./config/
COPY agent/prompts/ ./prompts/
COPY agent/pricing/ ./pricing/
COPY agent/evals/ ./evals/
COPY knowledge/ /app/knowledge/
COPY deploy/ /app/deploy/
COPY agent/Dockerfile ./
RUN mkdir -p evals/reports runtime && chown -R 10001:10001 evals/reports runtime
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
