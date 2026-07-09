"""Port interfaces (Protocols) for the application layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Optional, Protocol, Union

from .domain.models import ImuWindow, SecondAggregate, WindowFeatures


@dataclass(frozen=True)
class FrameError:
    """Marker yielded by a WindowSource when a single frame fails to parse.

    Lets the source keep its connection alive while letting the consumer count
    the failure, matching the original per-frame error handling.
    """

    error: Exception


# A WindowSource yields a parsed window, ``None`` (new-session marker), or a
# FrameError (a single frame that failed to parse).
WindowItem = Union[ImuWindow, None, FrameError]


class WindowSource(Protocol):
    """A source of IMU windows (e.g. a WS bridge), with internal reconnection."""

    def windows(self) -> AsyncIterator["WindowItem"]:
        """Yield ImuWindow instances, reconnecting on disconnect.

        Yields ``None`` each time a fresh session begins (so the consumer can
        reset per-session counters) and :class:`FrameError` when a single frame
        fails to parse without dropping the connection.
        """
        ...


class FeatureRepository(Protocol):
    """Persistence port for raw windows, features and aggregates."""

    async def persist(
        self,
        window: ImuWindow,
        features: WindowFeatures,
        aggregate: SecondAggregate,
    ) -> None:
        """Persist the raw window, its features and the 1-second aggregate."""
        ...
