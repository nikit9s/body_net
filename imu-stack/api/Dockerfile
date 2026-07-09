FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY imu_api/ ./imu_api/
ENV DB_DSN=postgresql://postgres:postgres@db:5432/imu
EXPOSE 8000
CMD ["uvicorn", "imu_api.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
