import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..utils import atomic_write_json

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["configuration"])


_app_state_lock = asyncio.Lock()
app_state = {
    'selected_users': ['LiveUser'],
    'stream_config': {
        'granularity': 'real-time'
    },
    'is_streaming': False,
    'current_data_timestamp': None,
    'live_history_window': '1hour',
}


def save_app_state():
    try:
        atomic_write_json(settings.APP_STATE_PATH, app_state)
        logger.debug(f"App state saved to {settings.APP_STATE_PATH}")
    except Exception as e:
        logger.error(f"Failed to save app state: {e}")


async def save_app_state_locked():
    async with _app_state_lock:
        await asyncio.to_thread(save_app_state)


def load_app_state():
    global app_state
    if settings.APP_STATE_PATH.exists():
        try:
            with open(settings.APP_STATE_PATH) as f:
                loaded = json.load(f)
                app_state.update(loaded)
            app_state['selected_users'] = ['LiveUser']
            logger.info(f"App state loaded from {settings.APP_STATE_PATH}")
        except Exception as e:
            logger.error(f"Failed to load app state: {e}")


class StreamConfig(BaseModel):
    granularity: str = "real-time"
    is_streaming: bool | None = None
    live_history_window: str | None = None


@router.post("/users")
async def configure_users(config: dict | None = None):
    return {
        'success': True,
        'users': ['LiveUser'],
        'count': 1
    }


@router.get("/users")
async def get_user_config():
    return {
        'users': ['LiveUser'],
    }


@router.post("/stream")
async def configure_stream(config: StreamConfig):
    try:
        async with _app_state_lock:
            app_state['stream_config'] = {
                'granularity': 'real-time'
            }

            if config.is_streaming is not None:
                app_state['is_streaming'] = config.is_streaming
                logger.info(f"Streaming status: {config.is_streaming}")

            if config.live_history_window:
                app_state['live_history_window'] = config.live_history_window
                logger.info(f"Live history window: {config.live_history_window}")

            await asyncio.to_thread(save_app_state)

        return {
            'success': True,
            'config': app_state['stream_config'],
            'is_streaming': app_state['is_streaming']
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Error configuring stream: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to configure stream.") from e


@router.get("/stream")
async def get_stream_config():
    return {
        'granularity': 'real-time',
        'is_streaming': app_state.get('is_streaming', False),
        'selected_users': ['LiveUser'],
        'live_history_window': app_state.get('live_history_window', '1hour')
    }


def get_app_state():
    return app_state


@router.get("/defaults")
async def get_defaults():
    from ..agent.llm_providers import _DEFAULT_MODELS
    from ..config import settings

    return {
        "llm_provider": settings.DEFAULT_LLM_PROVIDER,
        "model": settings.DEFAULT_MODEL,
        "data_source": "live",
        "provider_models": {k.value: v for k, v in _DEFAULT_MODELS.items()}
    }
