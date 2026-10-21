from __future__ import annotations

from fastapi import APIRouter, Depends

from .agent_diagnostics import diagnostics_router
from .agent_lifecycle import (
    lifecycle_router,
    shutdown_agents,
    start_system_ingestion,
    stop_all_ingestions,
    try_restore_agent,
)
from .agent_state import (
    active_agents,
    get_active_agents_dict,
    get_memory_manager_for,
    require_valid_ids,
)
from .agent_tasks import tasks_router
from .agent_threads import threads_router

router = APIRouter(prefix="/api/agent", tags=["agent"], dependencies=[Depends(require_valid_ids)])
router.include_router(lifecycle_router)
router.include_router(diagnostics_router)
router.include_router(tasks_router)
router.include_router(threads_router)
