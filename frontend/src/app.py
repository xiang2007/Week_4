import httpx
import io
import json
import os

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8080").rstrip("/")


def response_data(resp: httpx.Response):
    try:
        return resp.json()
    except json.JSONDecodeError:
        return None


def response_detail(resp: httpx.Response, fallback: str) -> str:
    data = response_data(resp)
    if isinstance(data, dict) and data.get("detail"):
        return str(data["detail"])
    text = resp.text.strip()
    return text or fallback


def create_app() -> FastAPI:
    app = FastAPI(title="Frontend Server")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(name="landing.html", request=request)

    @app.get("/app", response_class=HTMLResponse)
    async def app_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(name="index.html", request=request)

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(name="admin.html", request=request)

    return app


app = create_app()

@app.post("/convert-pdf")
async def convert_pdf(files: list[UploadFile] = File(...)):
    """Proxy uploaded receipt images to the backend for PDF conversion."""
    file_tuples = [
        ("files", (f.filename, f.file, f.content_type))
        for f in files
    ]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BACKEND_URL}/process",
            files=file_tuples
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Backend conversion failed")

    return StreamingResponse(
        io.BytesIO(resp.content),
        media_type="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=tax_receipts.pdf"
        }
    )

@app.post("/tax-summary") #add
async def tax_summary(request: Request):
    """Read tax relief summary from the logged-in user's backend database."""
    auth_header = request.headers.get("authorization")
    data = None
    if not auth_header:
        data = await request.json()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BACKEND_URL}/tax-summary",
            json=data,
            headers={"Authorization": auth_header} if auth_header else None,
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Backend tax summary failed"))

    return response_data(resp)


@app.post("/auth/signup")
async def signup(request: Request):
    data = await request.json()
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{BACKEND_URL}/auth/signup", json=data)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Signup failed"))

    return response_data(resp)


@app.post("/auth/login")
async def login(request: Request):
    data = await request.json()
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{BACKEND_URL}/auth/login", json=data)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Login failed"))

    return response_data(resp)


@app.post("/admin/auth/login")
async def admin_login(request: Request):
    data = await request.json()
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{BACKEND_URL}/admin/auth/login", json=data)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Admin login failed"))

    return response_data(resp)


@app.get("/admin/accounts")
async def admin_accounts(request: Request):
    auth_header = request.headers.get("authorization")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BACKEND_URL}/admin/accounts",
            headers={"Authorization": auth_header} if auth_header else None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to load accounts"))

    return response_data(resp)


@app.post("/admin/accounts/reset-password")
async def admin_reset_password(request: Request):
    auth_header = request.headers.get("authorization")
    data = await request.json()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BACKEND_URL}/admin/accounts/reset-password",
            json=data,
            headers={"Authorization": auth_header} if auth_header else None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to reset password"))

    return response_data(resp)


@app.get("/receipts")
async def get_receipts(request: Request):
    auth_header = request.headers.get("authorization")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BACKEND_URL}/receipts",
            headers={"Authorization": auth_header} if auth_header else None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to load receipts"))

    return response_data(resp)


@app.get("/ai-summary")
async def ai_summary(request: Request):
    auth_header = request.headers.get("authorization")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BACKEND_URL}/ai-summary",
            headers={"Authorization": auth_header} if auth_header else None,
            timeout=20,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to load AI summary"))

    return response_data(resp)


@app.post("/ai-chat")
async def ai_chat(request: Request):
    auth_header = request.headers.get("authorization")
    data = await request.json()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BACKEND_URL}/ai-chat",
            json=data,
            headers={"Authorization": auth_header} if auth_header else None,
            timeout=20,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "AI chat failed"))

    return response_data(resp)


@app.post("/receipts")
async def add_receipt(request: Request):
    auth_header = request.headers.get("authorization")
    data = await request.json()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BACKEND_URL}/receipts",
            json=data,
            headers={"Authorization": auth_header} if auth_header else None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to save receipt"))

    return response_data(resp)


@app.post("/receipts/batch")
async def add_receipts_batch(request: Request):
    auth_header = request.headers.get("authorization")
    data = await request.json()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BACKEND_URL}/receipts/batch",
            json=data,
            headers={"Authorization": auth_header} if auth_header else None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to save receipts"))

    return response_data(resp)


@app.delete("/receipts/{receipt_id}")
async def delete_receipt(receipt_id: int, request: Request):
    auth_header = request.headers.get("authorization")
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{BACKEND_URL}/receipts/{receipt_id}",
            headers={"Authorization": auth_header} if auth_header else None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to delete receipt"))

    return response_data(resp)
