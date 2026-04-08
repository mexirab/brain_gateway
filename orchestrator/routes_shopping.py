"""Shopping / Grocery List API routes."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/shopping")
async def get_shopping(list_name: str = None, include_checked: bool = False):
    """Get shopping list items."""
    from state_store import get_shopping_list

    items = get_shopping_list(list_name=list_name, include_checked=include_checked)
    return JSONResponse(items)


@router.post("/api/shopping")
async def add_shopping(req: Request):
    """Add an item to the shopping list."""
    from state_store import add_shopping_item

    body = await req.json()
    item = str(body.get("item", "")).strip()[:200]
    list_name = str(body.get("list_name", "grocery")).strip()[:50]
    if not item:
        return JSONResponse({"error": "Item is required"}, status_code=400)
    result = add_shopping_item(item, list_name)
    return JSONResponse(result)


@router.post("/api/shopping/{item_id}/check")
async def check_shopping(item_id: int):
    """Check off a shopping list item."""
    from state_store import check_shopping_item

    ok = check_shopping_item(item_id, checked=True)
    return JSONResponse({"ok": ok})


@router.post("/api/shopping/{item_id}/uncheck")
async def uncheck_shopping(item_id: int):
    """Uncheck a shopping list item."""
    from state_store import check_shopping_item

    ok = check_shopping_item(item_id, checked=False)
    return JSONResponse({"ok": ok})


@router.delete("/api/shopping/checked")
async def clear_checked(list_name: str = None):
    """Clear all checked items."""
    from state_store import clear_checked_items

    count = clear_checked_items(list_name)
    return JSONResponse({"ok": True, "cleared": count})


@router.delete("/api/shopping/{item_id}")
async def delete_shopping(item_id: int):
    """Delete a shopping list item."""
    from state_store import remove_shopping_item

    ok = remove_shopping_item(item_id)
    return JSONResponse({"ok": ok})
