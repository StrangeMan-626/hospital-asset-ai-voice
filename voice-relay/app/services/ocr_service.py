import asyncio
import base64

import ddddocr

from app.core.config import get_settings
from app.core.errors import ErrorCode, RelayError

_ocr = ddddocr.DdddOcr(beta=get_settings().OCR_BETA, show_ad=False)


async def recognize(image_base64: str) -> dict:
    if not image_base64:
        raise RelayError(400, "image is empty", ErrorCode.OCR_BAD_IMAGE)
    if "," in image_base64:
        image_base64 = image_base64.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(image_base64)
    except Exception as exc:
        raise RelayError(400, "invalid base64 image", ErrorCode.OCR_BAD_IMAGE) from exc

    end = image_bytes.find(b"IEND")
    if end >= 0:
        image_bytes = image_bytes[: end + 8]

    try:
        text = await asyncio.to_thread(_ocr.classification, image_bytes)
    except Exception as exc:
        raise RelayError(500, f"ocr failed: {exc}", ErrorCode.OCR_RECOGNIZE_FAIL) from exc

    return {"result": (text or "").strip()}
