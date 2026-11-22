# ---- Etapa de build ----
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ---- Etapa de runtime ----
FROM python:3.12-slim
RUN useradd --create-home appuser && mkdir -p /shared && chown appuser /shared
WORKDIR /app
COPY --from=builder /root/.local /home/appuser/.local
COPY ./src ./src
ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1
USER appuser
EXPOSE 8080
# python:3.12-slim no incluye curl: el health check usa Python
HEALTHCHECK --interval=15s --timeout=5s --retries=2 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
