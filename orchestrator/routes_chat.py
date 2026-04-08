"""Chat conversation history API routes."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/chat/conversations")
async def list_chat_conversations(limit: int = 50):
    """List all conversations, most recent first."""
    from state_store import list_conversations

    return JSONResponse(list_conversations(min(limit, 200)))


@router.post("/api/chat/conversations")
async def create_chat_conversation(request: Request):
    """Create a new conversation."""
    import uuid

    from state_store import create_conversation

    body = await request.json()
    title = body.get("title", "New Chat")
    conv_id = str(uuid.uuid4())
    return JSONResponse(create_conversation(conv_id, title))


@router.get("/api/chat/conversations/{conv_id}/messages")
async def get_chat_messages(conv_id: str):
    """Get all messages in a conversation."""
    from state_store import get_conversation, get_conversation_messages

    conv = get_conversation(conv_id)
    if not conv:
        return JSONResponse({"error": "Conversation not found"}, status_code=404)
    messages = get_conversation_messages(conv_id)
    return JSONResponse({"conversation": conv, "messages": messages})


@router.post("/api/chat/conversations/{conv_id}/messages")
async def add_chat_message(conv_id: str, request: Request):
    """Save a message to a conversation."""
    from state_store import get_conversation, save_chat_message

    conv = get_conversation(conv_id)
    if not conv:
        return JSONResponse({"error": "Conversation not found"}, status_code=404)
    body = await request.json()
    role = body.get("role", "user")
    content = body.get("content", "")
    routing = body.get("routing")
    announcement_type = body.get("announcement_type")
    if not content:
        return JSONResponse({"error": "No content"}, status_code=400)
    import json

    routing_str = json.dumps(routing) if routing else None
    msg = save_chat_message(conv_id, role, content, routing_str, announcement_type)
    return JSONResponse(msg)


@router.put("/api/chat/conversations/{conv_id}")
async def update_chat_conversation(conv_id: str, request: Request):
    """Update conversation title."""
    from state_store import update_conversation_title

    body = await request.json()
    title = body.get("title", "")
    if not title:
        return JSONResponse({"error": "No title"}, status_code=400)
    ok = update_conversation_title(conv_id, title)
    if not ok:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.delete("/api/chat/conversations/{conv_id}")
async def delete_chat_conversation(conv_id: str):
    """Delete a conversation and all its messages."""
    from state_store import delete_conversation

    ok = delete_conversation(conv_id)
    if not ok:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"ok": True})
