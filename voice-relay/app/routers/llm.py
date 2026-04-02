from fastapi import APIRouter, Request
from app.services import llm_service

router = APIRouter(tags=["LLM"])


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.body()
    return await llm_service.chat_completions(body, trace_id=request.headers.get("x-trace-id", ""))
