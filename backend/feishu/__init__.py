from __future__ import annotations

from .cards import build_back_card, build_evidence_card, build_plain_card
from .models import MessageChannel, MessageEnvelope
from .sender import FeishuSender

__all__ = [
    "FeishuSender",
    "MessageChannel",
    "MessageEnvelope",
    "build_evidence_card",
    "build_back_card",
    "build_plain_card",
]


def __getattr__(name: str):
    if name == "FeishuGateway":
        from .gateway import FeishuGateway as _G
        return _G
    raise AttributeError(name)
