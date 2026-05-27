# ddddocr 接入开发说明

## 目标

在 `voice-relay` 项目内直接接入 `ddddocr`，提供本地验证码识别 HTTP 接口，不再拆分独立 Python OCR 项目。

## 接入方式

- 在当前 FastAPI 服务内新增一个 OCR 路由
- 路由接收 `base64` 图片
- 服务层调用 `ddddocr` 识别验证码
- 返回统一 JSON：`{"result":"验证码文本"}`

## 需要修改的文件

- `requirements.txt`
- `app/core/config.py`
- `app/core/errors.py`
- `app/main.py`
- `app/routers/ocr.py`
- `app/services/ocr_service.py`

## 依赖

在 `requirements.txt` 追加：

```txt
ddddocr
```

## 配置项

在 `app/core/config.py` 的 `Settings` 中新增：

```python
    OCR_ENABLED: bool = True
    OCR_BETA: bool = False
```

说明：

- `OCR_ENABLED`：是否启用 OCR 路由
- `OCR_BETA`：是否启用 `ddddocr.DdddOcr(beta=True)` 模式

在 `.env.example` 追加：

```env
OCR_ENABLED=true
OCR_BETA=false
```

## 路由设计

新增文件：`app/routers/ocr.py`

路由：

- `POST /ocr/recognize`

请求体：

```json
{
  "image": "base64图片字符串"
}
```

返回体：

```json
{
  "result": "abcd"
}
```

异常返回沿用当前项目统一错误格式：

```json
{
  "error": {
    "code": "OCR_BAD_IMAGE",
    "message": "image is empty"
  }
}
```

## 服务设计

新增文件：`app/services/ocr_service.py`

职责：

- 单例初始化 `ddddocr.DdddOcr`
- 对传入的 `base64` 做解码
- 调用 `classification(image_bytes)` 返回识别结果
- 对空图、非法 base64、识别失败做统一异常转换

建议实现要点：

- OCR 实例在模块级初始化，避免每次请求重复加载模型
- `classification()` 属于 CPU 计算，接口层建议用 `asyncio.to_thread()` 包一层
- 识别结果统一 `strip()`

## 错误码

在 `app/core/errors.py` 的 `ErrorCode` 中新增：

```python
    OCR_BAD_IMAGE = "OCR_BAD_IMAGE"
    OCR_RECOGNIZE_FAIL = "OCR_RECOGNIZE_FAIL"
```

建议映射：

- 图片为空：`400 + OCR_BAD_IMAGE`
- base64 非法：`400 + OCR_BAD_IMAGE`
- OCR 执行失败：`500 + OCR_RECOGNIZE_FAIL`

## main.py 改动

在 `app/main.py` 中新增导入并注册：

```python
from app.routers import ocr
```

```python
if get_settings().OCR_ENABLED:
    app.include_router(ocr.router)
```

## 路由入参与返回建议

请求模型：

```python
from pydantic import BaseModel


class OcrRequest(BaseModel):
    image: str
```

返回：

```python
{"result": text}
```

## 参考实现骨架

`app/routers/ocr.py`

```python
from fastapi import APIRouter
from pydantic import BaseModel

from app.services import ocr_service

router = APIRouter(prefix="/ocr", tags=["OCR"])


class OcrRequest(BaseModel):
    image: str


@router.post("/recognize")
async def recognize(payload: OcrRequest):
    return await ocr_service.recognize(payload.image)
```

`app/services/ocr_service.py`

```python
import asyncio
import base64

import ddddocr

from app.core.config import get_settings
from app.core.errors import ErrorCode, RelayError

_ocr = ddddocr.DdddOcr(beta=get_settings().OCR_BETA, show_ad=False)


async def recognize(image_base64: str) -> dict:
    if not image_base64:
        raise RelayError(400, "image is empty", ErrorCode.OCR_BAD_IMAGE)
    try:
        image_bytes = base64.b64decode(image_base64)
    except Exception as exc:
        raise RelayError(400, "invalid base64 image", ErrorCode.OCR_BAD_IMAGE) from exc

    try:
        text = await asyncio.to_thread(_ocr.classification, image_bytes)
    except Exception as exc:
        raise RelayError(500, f"ocr failed: {exc}", ErrorCode.OCR_RECOGNIZE_FAIL) from exc

    return {"result": (text or "").strip()}
```

## 开发顺序

1. `requirements.txt` 增加 `ddddocr`
2. `config.py` 增加 OCR 开关配置
3. `errors.py` 增加 OCR 错误码
4. 新增 `ocr_service.py`
5. 新增 `ocr.py`
6. `main.py` 注册路由
7. `.env.example` 增加配置项

## 备注

- 当前项目本身就是 Python，直接内嵌 `ddddocr` 最合适
- 如果后续识别率不够，再增加图片预处理，不要先过度设计
