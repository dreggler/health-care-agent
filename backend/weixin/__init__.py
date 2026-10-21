from __future__ import annotations

from .models import MessageChannel, MessageEnvelope
from .sender import WeixinSender

__all__ = ["WeixinSender", "MessageChannel", "MessageEnvelope"]


def __getattr__(name: str):
    if name == "WeixinGateway":
        from .gateway import WeixinGateway as _G
        return _G
    raise AttributeError(name)
