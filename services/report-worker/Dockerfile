FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:3.12-slim
RUN useradd --create-home appuser
WORKDIR /app
COPY --from=builder /root/.local /home/appuser/.local
COPY ./src ./src
ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1
USER appuser
CMD ["python", "-m", "src.worker"]
