FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir asyncpg websockets numpy
WORKDIR /app
COPY imu_ingest_worker.py .
CMD ["python", "-u", "imu_ingest_worker.py"]
