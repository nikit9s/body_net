.PHONY: up down logs ps bridge bridge-host

up:
	docker compose up -d --build

down:
	docker compose down -v

logs:
	docker compose logs -f --tail=200

ps:
	docker compose ps

# Linux only — starts bridge in Docker with BLE access
bridge:
	docker compose --profile ble up -d --build bridge

# macOS / any host — runs bridge directly (needs venv)
bridge-host:
	cd bridge && python3 bridge_ble_ws.py
