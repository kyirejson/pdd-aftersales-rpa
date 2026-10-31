from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ConnectorResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    error: str | None = None


class Connector(ABC):
    name: str
    supports_live_writes: bool = False

    @abstractmethod
    def health(self) -> ConnectorResult:
        raise NotImplementedError


class PddConnector(Connector, ABC):
    @abstractmethod
    def discover(self, shop: dict[str, Any]) -> ConnectorResult:
        raise NotImplementedError


class JstConnector(Connector, ABC):
    @abstractmethod
    def query_order(self, order_no: str, tracking_no: str = "") -> ConnectorResult:
        raise NotImplementedError


class LogisticsConnector(Connector, ABC):
    @abstractmethod
    def query(self, carrier: str, tracking_no: str) -> ConnectorResult:
        raise NotImplementedError


class MessageConnector(Connector, ABC):
    @abstractmethod
    def preview_send(self, group_name: str, message: str) -> ConnectorResult:
        raise NotImplementedError

    def send(self, group: dict[str, Any], message: str) -> ConnectorResult:
        """Send one live interception message when a desktop connector exists."""
        return ConnectorResult(
            False,
            {"group": group, "message": message, "sent": False},
            error="当前消息连接器未配置真实桌面发送组件",
        )
