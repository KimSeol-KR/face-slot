"""얼굴 슬롯머신 — FastAPI 백엔드.

흐름: 사진 업로드 → 서버가 랜덤 속성 조합을 뽑음 → 이미지 편집 모델로
"얼굴만 가상의 인물로" 교체 → 결과 이미지 + 뽑힌 속성을 반환.

- 업로드 이미지는 디스크에 저장하지 않고 메모리에서만 처리한다.
- GEMINI_API_KEY가 없으면 모의(mock) 모드로 동작한다(원본을 그대로 돌려줌).
"""

import asyncio
import base64
import io
import os
import random
import time
from collections import defaultdict, deque
from datetime import date
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "8")) * 1024 * 1024
MAX_SIDE = int(os.getenv("MAX_SIDE", "1024"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "4"))
MAX_DAILY_SPINS = int(os.getenv("MAX_DAILY_SPINS", "300"))

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

# 릴(슬롯)별 후보. 라벨은 화면에 표시되고, prompt는 모델에 전달된다.
REELS: dict[str, list[dict[str, str]]] = {
    "연령대": [
        {"label": "10대 후반", "prompt": "late teens"},
        {"label": "20대", "prompt": "in their twenties"},
        {"label": "30대", "prompt": "in their thirties"},
        {"label": "40대", "prompt": "in their forties"},
        {"label": "50대", "prompt": "in their fifties"},
        {"label": "60대", "prompt": "in their sixties"},
        {"label": "70대", "prompt": "in their seventies"},
    ],
    "지역 느낌": [
        {"label": "동아시아", "prompt": "East Asian features"},
        {"label": "동남아시아", "prompt": "Southeast Asian features"},
        {"label": "남아시아", "prompt": "South Asian features"},
        {"label": "중동·북아프리카", "prompt": "Middle Eastern or North African features"},
        {"label": "서아프리카", "prompt": "West African features"},
        {"label": "동아프리카", "prompt": "East African features"},
        {"label": "북유럽", "prompt": "Northern European features"},
        {"label": "남유럽", "prompt": "Southern European features"},
        {"label": "라틴아메리카", "prompt": "Latin American features"},
        {"label": "중앙아시아", "prompt": "Central Asian features"},
        {"label": "혼혈 느낌", "prompt": "mixed-heritage features"},
    ],
    "헤어": [
        {"label": "짧은 단발", "prompt": "a short bob haircut"},
        {"label": "긴 생머리", "prompt": "long straight hair"},
        {"label": "곱슬머리", "prompt": "curly hair"},
        {"label": "스포츠머리", "prompt": "a buzz cut"},
        {"label": "올백", "prompt": "slicked-back hair"},
        {"label": "파마 웨이브", "prompt": "wavy permed hair"},
        {"label": "백발", "prompt": "silver-white hair"},
        {"label": "삭발", "prompt": "a completely shaved head"},
        {"label": "땋은 머리", "prompt": "braided hair"},
        {"label": "앞머리 있는 중단발", "prompt": "a medium-length cut with bangs"},
    ],
    "표정": [
        {"label": "활짝 웃음", "prompt": "a big genuine smile"},
        {"label": "무표정", "prompt": "a neutral, blank expression"},
        {"label": "수줍은 미소", "prompt": "a shy, slight smile"},
        {"label": "놀란 표정", "prompt": "a surprised expression"},
        {"label": "근엄함", "prompt": "a stern, serious expression"},
        {"label": "장난꾸러기", "prompt": "a playful smirk"},
        {"label": "졸린 눈", "prompt": "sleepy half-closed eyes"},
    ],
    "분위기": [
        {"label": "다정한 이웃", "prompt": "a warm, friendly neighbor vibe"},
        {"label": "카리스마", "prompt": "a charismatic, commanding presence"},
        {"label": "엉뚱한 예술가", "prompt": "a quirky artist vibe"},
        {"label": "모범생", "prompt": "a studious, tidy vibe"},
        {"label": "터프한 장인", "prompt": "a rugged craftsperson vibe"},
        {"label": "도시 힙스터", "prompt": "an urban hipster vibe"},
        {"label": "고요한 현자", "prompt": "a calm, wise vibe"},
    ],
}

PROMPT_TEMPLATE = (
    "Edit this photo. Replace ONLY the face of the person in the photo with the face of a "
    "completely fictional person who does not exist. Keep everything else unchanged: "
    "background, clothing, body, pose, camera angle, lighting, color grading and framing.\n"
    "The new fictional face should have these traits: {traits}.\n"
    "Rules: the new face must be an original invention. It must NOT resemble the original "
    "person, and must NOT resemble any real person, celebrity, politician or public figure. "
    "Blend skin tone, neck and hairline naturally so the result looks like one coherent "
    "photograph. Output a single photorealistic image."
)

app = FastAPI(title="Face Slot", docs_url=None, redoc_url=None)

_rng = random.SystemRandom()
_hits: dict[str, deque] = defaultdict(deque)
_daily = {"day": date.today(), "count": 0}
_lock = asyncio.Lock()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def check_limits(ip: str) -> None:
    async with _lock:
        now = time.time()
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            raise HTTPException(429, "너무 빨라요. 잠시 후 다시 돌려주세요.")
        if _daily["day"] != date.today():
            _daily.update(day=date.today(), count=0)
        if _daily["count"] >= MAX_DAILY_SPINS:
            raise HTTPException(429, "오늘의 슬롯 횟수가 모두 소진되었어요. 내일 다시 와주세요.")
        q.append(now)
        _daily["count"] += 1


def prepare_image(raw: bytes) -> tuple[bytes, str]:
    """업로드 바이트를 검증하고, EXIF 회전 적용 + 리사이즈 후 JPEG로 정규화."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise HTTPException(400, "이미지 파일을 읽을 수 없어요. JPG/PNG/WEBP 사진을 올려주세요.")
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), "image/jpeg"


def pick_attributes() -> dict[str, dict[str, str]]:
    return {name: _rng.choice(options) for name, options in REELS.items()}


def build_prompt(picked: dict[str, dict[str, str]]) -> str:
    traits = "; ".join(v["prompt"] for v in picked.values())
    return PROMPT_TEMPLATE.format(traits=traits)


async def edit_with_gemini(image_bytes: bytes, mime: str, prompt: str) -> tuple[bytes, str]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_IMAGE_MODEL}:generateContent"
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode()}},
                ]
            }
        ],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY})
    except httpx.HTTPError:
        raise HTTPException(502, "이미지 생성 서버에 연결하지 못했어요. 잠시 후 다시 시도해주세요.")

    if resp.status_code != 200:
        raise HTTPException(502, f"이미지 생성에 실패했어요. (상태 코드 {resp.status_code})")

    data = resp.json()
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                out_mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                return base64.b64decode(inline["data"]), out_mime

    # 이미지가 없으면 안전 필터 또는 얼굴 미검출일 가능성이 높다.
    raise HTTPException(422, "이 사진은 변환할 수 없었어요. 얼굴이 또렷하게 보이는 다른 사진으로 시도해주세요.")


@app.get("/api/options")
async def options():
    return {"reels": {name: [o["label"] for o in opts] for name, opts in REELS.items()}}


@app.get("/api/health")
async def health():
    return {"ok": True, "mock": not GEMINI_API_KEY}


@app.post("/api/spin")
async def spin(request: Request, photo: UploadFile = File(...)):
    await check_limits(client_ip(request))

    raw = await photo.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"사진은 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 이하로 올려주세요.")
    image_bytes, mime = prepare_image(raw)

    picked = pick_attributes()
    prompt = build_prompt(picked)
    attrs = {name: v["label"] for name, v in picked.items()}

    if not GEMINI_API_KEY:
        await asyncio.sleep(3)  # 모의 모드: 릴 연출 확인용 지연
        out_bytes, out_mime, mock = image_bytes, mime, True
    else:
        out_bytes, out_mime = await edit_with_gemini(image_bytes, mime, prompt)
        mock = False

    data_url = f"data:{out_mime};base64,{base64.b64encode(out_bytes).decode()}"
    return JSONResponse({"image": data_url, "attrs": attrs, "mock": mock})


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
