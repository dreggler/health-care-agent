from __future__ import annotations

import logging
from collections.abc import Iterable

from .base import BaseGateway, MessageChannel, MessageEnvelope

logger = logging.getLogger(__name__)


class GatewayRegistry:

    def __init__(self) -> None:
        self._gateways: dict[MessageChannel, BaseGateway] = {}


    def register(self, gateway: BaseGateway) -> None:
        channel = getattr(gateway, "channel", None)
        if channel is None:
            raise ValueError(
                f"Gateway {type(gateway).__name__} has no 'channel' attribute"
            )
        if channel in self._gateways:
            logger.warning(
                "GatewayRegistry: replacing existing gateway for channel %s",
                channel,
            )
        self._gateways[channel] = gateway
        logger.info("GatewayRegistry: registered %s gateway", channel.value)

    def unregister(self, channel: MessageChannel) -> None:
        self._gateways.pop(channel, None)

    def clear(self) -> None:
        self._gateways.clear()


    def get(self, channel: MessageChannel) -> BaseGateway | None:
        return self._gateways.get(channel)

    def all(self) -> list[BaseGateway]:
        return list(self._gateways.values())

    def channels(self) -> Iterable[MessageChannel]:
        return self._gateways.keys()

    def for_envelope(self, envelope: MessageEnvelope) -> BaseGateway | None:
        gw = self._gateways.get(envelope.channel)
        if gw is not None:
            return gw
        if self._gateways:
            return next(iter(self._gateways.values()))
        return None


    def __len__(self) -> int:
        return len(self._gateways)

    def __contains__(self, channel: MessageChannel) -> bool:
        return channel in self._gateways
