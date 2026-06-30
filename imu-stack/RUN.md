# Что нужно установить

- **Docker Desktop** (включает `docker` и `docker compose`) — для стека `db / worker / api / nginx`. Должен быть запущен перед `make up`.
- **make** — на macOS ставится вместе с Xcode Command Line Tools: `xcode-select --install`.
- **Python 3.10+** — для моста `bridge` (на хосте, т.к. нужен прямой доступ к Bluetooth). Проверить: `python3 --version`.
- **Bluetooth** — включённый адаптер на хосте; при первом запуске моста macOS попросит дать терминалу/IDE доступ к Bluetooth (System Settings → Privacy & Security → Bluetooth).
- *(опционально)* **TablePlus** или другой клиент Postgres — чтобы подключаться к БД по ссылке из шага 3.

# Запуск

1. ```bash 
    make up
```

2. ```bash
    cd /Users/kirill/work/body_net/imu-stack/bridge
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python bridge_ble_ws.py
```

3. URL для подключения к бд через tableplus
```
postgresql://postgres:postgres@127.0.0.1:5433/imu?statusColor=DAEBC2&env=local&name=body_net&tLSMode=0&usePrivateKey=false&safeModeLevel=0&advancedSafeModeLevel=0&driverVersion=0&lazyload=false
```