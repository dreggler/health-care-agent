import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)

class ConnectionManager:

    def __init__(self):
        self.active_connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.debug(f"WebSocket connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)
        logger.debug(f"WebSocket disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.warning(f"Failed to broadcast to {connection}: {e}")
                self.disconnect(connection)

    def is_connected(self, websocket: WebSocket) -> bool:
        return websocket in self.active_connections

    def count(self) -> int:
        return len(self.active_connections)

data_stream_manager = ConnectionManager()
agent_monitor_manager = ConnectionManager()
