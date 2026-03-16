from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from app.core.errors import RelayError
from app.services import tts_service

router = APIRouter(prefix="/tts", tags=["TTS"])


class TTSRequest(BaseModel):
    text: str
    speed: float = Field(default=1.0)


@router.post("/synthesize")
async def synthesize(req: TTSRequest):
    if not req.text or not req.text.strip():
        raise RelayError(400, "text is empty")
    if len(req.text) > 20000:
        raise RelayError(400, "text exceeds 20000 characters")
    audio = await tts_service.synthesize(req.text)
    return Response(content=audio, media_type="audio/mpeg")
