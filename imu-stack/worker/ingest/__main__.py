"""Composition root: build Settings, wire adapters, run, handle signals."""

from __future__ import annotations

import asyncio
import logging
import signal

from .adapters.timescale_repo import TimescaleFeatureRepository
from .adapters.ws_source import WebSocketWindowSource
from .application.service import IngestService
from .config import Settings
from .logging_setup import setup_logging


async def run(settings: Settings, log: logging.Logger) -> None:
    """Build the repository and source, wire the service and run it."""
    repository = await TimescaleFeatureRepository.create(settings.db_dsn)
    log.info("db pool created  dsn=%s", settings.db_dsn.split("@")[-1])

    source = WebSocketWindowSource(
        ws_url=settings.ws_url,
        reconnect_s=settings.ws_reconnect_s,
        log=log,
    )
    service = IngestService(source=source, repository=repository, log=log)

    try:
        await service.run()
    finally:
        await repository.close()


def main() -> None:
    """Run the ingest worker with graceful SIGINT/SIGTERM shutdown."""
    settings = Settings.from_env()
    log = setup_logging()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(run(settings, log))

    def _shutdown() -> None:
        log.info("signal received, cancelling…")
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(task)
    finally:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == "__main__":
    main()
