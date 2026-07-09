"""IngestService: pull windows, compute features, persist them."""

from __future__ import annotations

import logging
import time

from ..domain.features import compute_features
from ..domain.models import ImuWindow
from ..ports import FeatureRepository, FrameError, WindowSource


class IngestService:
    """Use case coordinating the IMU ingest pipeline.

    Depends only on the ``WindowSource`` and ``FeatureRepository`` ports plus a
    logger. Pulls windows from the source, computes features via the domain
    layer, persists them, and emits progress/counter logging.
    """

    def __init__(
        self,
        source: WindowSource,
        repository: FeatureRepository,
        log: logging.Logger,
    ) -> None:
        self._source = source
        self._repository = repository
        self._log = log
        self.frames_total = 0
        self.errors_total = 0

    async def run(self) -> None:
        """Consume windows until the source is exhausted or cancelled."""
        session_frames = 0
        session_start = time.monotonic()

        async for item in self._source.windows():
            if item is None:
                # Marker for a fresh WS session: reset per-session counters.
                session_frames = 0
                session_start = time.monotonic()
                continue
            if isinstance(item, FrameError):
                # A frame that failed to parse upstream.
                self.errors_total += 1
                self._log.error("frame processing error: %s", item.error)
                continue
            try:
                await self._process(item)
                self.frames_total += 1
                session_frames += 1
                if session_frames % 100 == 0:
                    elapsed = time.monotonic() - session_start
                    rate = session_frames / elapsed if elapsed > 0 else 0
                    self._log.info(
                        "progress  session=%d  total=%d  rate=%.1f frames/s  errors=%d",
                        session_frames, self.frames_total, rate, self.errors_total)
            except Exception as e:
                self.errors_total += 1
                self._log.error("frame processing error: %s", e)

        self._log.info("stopped  total_frames=%d  total_errors=%d",
                       self.frames_total, self.errors_total)

    async def _process(self, window: ImuWindow) -> None:
        """Compute features for one window and persist everything."""
        features, aggregate = compute_features(window)
        await self._repository.persist(window, features, aggregate)
