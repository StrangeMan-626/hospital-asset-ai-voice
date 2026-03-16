from fastapi import APIRouter, UploadFile, File
from app.core.errors import RelayError
from app.services import asr_service

router = APIRouter(prefix="/asr", tags=["ASR"])


@router.post("/recognize")
async def recognize(audio: UploadFile = File(...)):
    data = await audio.read()
    if not data:
        raise RelayError(400, "audio file is empty")
    return await asr_service.recognize(data, audio.filename or "audio.wav")
