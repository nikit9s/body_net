FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge_ble_ws.py ./
# На Linux для доступа к BLE внутри контейнера — проще запускать bridge на хосте.
# Но если очень надо в контейнере: --net=host + привилегии BLE.
CMD ["python", "-u", "bridge_ble_ws.py"]
