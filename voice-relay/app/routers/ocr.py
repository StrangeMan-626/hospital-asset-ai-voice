from fastapi import APIRouter
from pydantic import BaseModel

from app.services import ocr_service

router = APIRouter(prefix="/ocr", tags=["OCR"])


class OcrRequest(BaseModel):
    image: str


@router.post("/recognize")
async def recognize(payload: OcrRequest):
    return await ocr_service.recognize(payload.image)
