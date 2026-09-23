from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.models import CartItemRequest, ChatRequest, SpecPrepareRequest
from app.services.assistant import AssistantService, is_explicit_confirmation
from app.services.cart import CartError, DemoCartService
from app.services.ekt_client import EktApiError, EktClient
from app.services.files import FileExtractionError, extract_upload
from app.services.terms import TermsService
from app.services.spec import SpecError, SpecService

settings = get_settings()
ekt = EktClient(settings)
carts = DemoCartService(settings, ekt)
terms = TermsService()
assistant = AssistantService(settings, ekt, carts, terms)
specs = SpecService(settings, ekt, carts)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


app = FastAPI(title="EKT AI Assistant", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "openai_configured": bool(settings.openai_api_key),
            "catalog_configured": bool(settings.ekt_api_password)}


@app.get("/api/products")
async def search_products(q: str = Query(min_length=1), limit: int = Query(5, ge=1, le=20)) -> dict:
    try:
        return {"items": [item.model_dump() for item in await ekt.search_products(q, limit)]}
    except EktApiError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/products/{product_id}")
async def product_detail(product_id: int) -> dict:
    try:
        return (await ekt.get_product(product_id)).model_dump()
    except (EktApiError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/chat")
async def chat(request: ChatRequest) -> dict:
    try:
        return await assistant.chat(request)
    except Exception as exc:
        raise HTTPException(500, f"Assistant error: {exc}") from exc


@app.post("/api/files/extract")
async def upload_file(file: UploadFile = File(...)) -> dict:
    try:
        return await extract_upload(file, settings)
    except FileExtractionError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/spec/analyze")
async def analyze_specification(file: UploadFile = File(...), instructions: str = Form("")) -> dict:
    try:
        return await specs.analyze_upload(file, instructions)
    except (FileExtractionError, SpecError, EktApiError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/spec/prepare-cart")
async def prepare_specification_cart(request: SpecPrepareRequest) -> dict:
    try:
        return await specs.prepare_cart(request.session_id, request.analysis_id, request.line_ids)
    except (SpecError, CartError, EktApiError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/cart/prepare")
async def prepare_cart(session_id: str, items: list[CartItemRequest]) -> dict:
    try:
        return (await carts.prepare(session_id, items)).model_dump()
    except (CartError, EktApiError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/cart/confirm")
async def confirm_cart(session_id: str, pending_id: str, confirmation: str) -> dict:
    if not is_explicit_confirmation(confirmation):
        raise HTTPException(400, "Требуется явное подтверждение добавления")
    try:
        return await carts.commit(session_id, pending_id, confirmed=True)
    except (CartError, EktApiError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/cart")
async def get_cart(session_id: str) -> dict:
    return carts.get_cart(session_id)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path("app/static/index.html"))


@app.get("/cart.html")
async def cart_page() -> FileResponse:
    return FileResponse(Path("app/static/cart.html"))
