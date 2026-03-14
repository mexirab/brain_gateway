"""
Local Agent Interface for Brain Gateway.

Encapsulates all local infrastructure capabilities that the cloud brain
calls into: RAG, tool execution, reminders, focus, health.

Today this is a Python class called in-process. In a future phase, these
methods become HTTP/WebSocket endpoints served by the on-premise agent.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class LocalAgent:
    """
    Interface to local infrastructure. Cloud brain calls these methods.

    Wraps existing functions — no logic duplication. Each method delegates
    to the actual implementation in the orchestrator modules.
    """

    def __init__(
        self, rag_context_fn, run_tool_loop_fn, get_nemotron_system_prompt_fn, ha_client, collection, scheduler, profile
    ):
        """
        Args:
            rag_context_fn: callable(query: str) -> str — from orchestrator.py
            run_tool_loop_fn: callable(messages, system, label) -> str — from orchestrator.py
            get_nemotron_system_prompt_fn: callable(mode, intensity) -> str
            ha_client: HomeAssistantClient instance
            collection: ChromaDB collection
            scheduler: APScheduler instance
            profile: UserProfile instance
        """
        self._rag_context = rag_context_fn
        self._run_tool_loop = run_tool_loop_fn
        self._get_nemotron_system_prompt = get_nemotron_system_prompt_fn
        self._ha_client = ha_client
        self._collection = collection
        self._scheduler = scheduler
        self._profile = profile

    # --- RAG ---

    def rag_search(self, query: str) -> str:
        """Query ChromaDB and return formatted context string."""
        return self._rag_context(query)

    # --- Tool Execution ---

    async def execute_orchestrator(self, command: str, mode: str = "explainer", intensity: str = "low") -> str:
        """
        Run the Nemotron tool loop for a command.

        This is the equivalent of call_nemotron_orchestrator() — sends a
        command to Nemotron for tool execution and returns the result.
        """
        logger.info(f"[LOCAL_AGENT] Executing: {command[:100]}...")
        messages = [{"role": "user", "content": command}]
        system_prompt = self._get_nemotron_system_prompt(mode=mode, intensity=intensity)
        return await self._run_tool_loop(messages, system_prompt, label="ORCHESTRATOR")

    # --- State Queries ---

    def get_pending_reminders(self) -> List[Dict[str, Any]]:
        """Get all pending reminders from persistent store."""
        import state_store

        return state_store.get_pending_reminders()

    def get_focus_status(self) -> Dict[str, Any]:
        """Get current focus session state from persistent store."""
        import state_store

        session = state_store.load_focus_session()
        if not session["active"]:
            return {"active": False}
        elapsed = (datetime.now() - session["started"]).total_seconds() / 60
        return {
            "active": True,
            "task": session["task"],
            "remaining_minutes": max(0, session["duration"] - elapsed),
            "audio_player": session.get("audio_player"),
        }

    # --- Health ---

    async def health(self) -> Dict[str, Any]:
        """Local agent health: HA entities, RAG docs, scheduler."""
        return {
            "ha_entities": len(self._ha_client._entities),
            "rag_docs": self._collection.count(),
            "scheduler_running": self._scheduler.running,
            "scheduled_jobs": len(self._scheduler.get_jobs()),
        }
