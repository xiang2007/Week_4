import httpx
import io
import json
import os

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8080").rstrip("/")
BACKEND_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
SESSION_COOKIE_NAME = "receipt_manager_session"
ADMIN_SESSION_COOKIE_NAME = "receipt_manager_admin_session"


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


def session_headers(request: Request, admin: bool = False) -> dict[str, str]:
    cookie_name = ADMIN_SESSION_COOKIE_NAME if admin else SESSION_COOKIE_NAME
    session_id = request.cookies.get(cookie_name)
    if not session_id:
        return {}
    return {"Cookie": f"{cookie_name}={session_id}"}


def cookie_json_response(resp: httpx.Response) -> JSONResponse:
    response = JSONResponse(content=response_data(resp))
    for cookie in resp.headers.get_list("set-cookie"):
        response.headers.append("set-cookie", cookie)
    return response


def create_app() -> FastAPI:
    app = FastAPI(title="Frontend Server")

    @app.exception_handler(httpx.RequestError)
    async def backend_connection_error(request: Request, exc: httpx.RequestError):
        return JSONResponse(
            status_code=502,
            content={
                "detail": f"Could not connect to backend at {BACKEND_URL}. Check the frontend BACKEND_URL environment variable."
            },
        )

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
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
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
    headers = session_headers(request)
    data = None
    if not headers:
        data = await request.json()

    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.post(
            f"{BACKEND_URL}/tax-summary",
            json=data,
            headers=headers or None,
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Backend tax summary failed"))

    return response_data(resp)


@app.post("/auth/signup")
async def signup(request: Request):
    data = await request.json()
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.post(f"{BACKEND_URL}/auth/signup", json=data)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Signup failed"))

    return cookie_json_response(resp)


@app.post("/auth/login")
async def login(request: Request):
    data = await request.json()
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.post(f"{BACKEND_URL}/auth/login", json=data)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Login failed"))

    return cookie_json_response(resp)


@app.get("/auth/me")
async def auth_me(request: Request):
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.get(f"{BACKEND_URL}/auth/me", headers=session_headers(request) or None)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Login required"))

    return response_data(resp)


@app.post("/auth/logout")
async def logout(request: Request):
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.post(f"{BACKEND_URL}/auth/logout", headers=session_headers(request) or None)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Logout failed"))

    return cookie_json_response(resp)


@app.post("/admin/auth/login")
async def admin_login(request: Request):
    data = await request.json()
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.post(f"{BACKEND_URL}/admin/auth/login", json=data)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Admin login failed"))

    return cookie_json_response(resp)


@app.get("/admin/auth/me")
async def admin_auth_me(request: Request):
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.get(f"{BACKEND_URL}/admin/auth/me", headers=session_headers(request, admin=True) or None)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Admin login required"))

    return response_data(resp)


@app.post("/admin/auth/logout")
async def admin_logout(request: Request):
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.post(f"{BACKEND_URL}/admin/auth/logout", headers=session_headers(request, admin=True) or None)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Admin logout failed"))

    return cookie_json_response(resp)


@app.get("/admin/accounts")
async def admin_accounts(request: Request):
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.get(
            f"{BACKEND_URL}/admin/accounts",
            headers=session_headers(request, admin=True) or None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to load accounts"))

    return response_data(resp)


@app.get("/admin/insights")
async def admin_insights(request: Request):
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.get(
            f"{BACKEND_URL}/admin/insights",
            headers=session_headers(request, admin=True) or None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to load insights"))

    return response_data(resp)


@app.get("/admin/ai-events")
async def admin_ai_events(request: Request):
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.get(
            f"{BACKEND_URL}/admin/ai-events",
            headers=session_headers(request, admin=True) or None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to load AI event logs"))

    return response_data(resp)


@app.get("/admin/ocr-receipts")
async def admin_ocr_receipts(request: Request):
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.get(
            f"{BACKEND_URL}/admin/ocr-receipts",
            headers=session_headers(request, admin=True) or None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to load OCR receipts"))

    return response_data(resp)


@app.post("/admin/accounts/reset-password")
async def admin_reset_password(request: Request):
    data = await request.json()
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.post(
            f"{BACKEND_URL}/admin/accounts/reset-password",
            json=data,
            headers=session_headers(request, admin=True) or None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to reset password"))

    return response_data(resp)


@app.get("/receipts")
async def get_receipts(request: Request):
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.get(
            f"{BACKEND_URL}/receipts",
            headers=session_headers(request) or None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to load receipts"))

    return response_data(resp)


@app.get("/ocr-config")
async def ocr_config(request: Request):
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.get(
            f"{BACKEND_URL}/ocr-config",
            headers=session_headers(request) or None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to load OCR settings"))

    return response_data(resp)


@app.get("/categories")
async def categories():
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.get(f"{BACKEND_URL}/categories")

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to load categories"))

    return response_data(resp)


@app.get("/ai-summary")
async def ai_summary(request: Request):
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.get(
            f"{BACKEND_URL}/ai-summary",
            headers=session_headers(request) or None,
            timeout=20,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to load AI summary"))

    return response_data(resp)


@app.post("/ai-chat")
async def ai_chat(request: Request):
    data = await request.json()
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.post(
            f"{BACKEND_URL}/ai-chat",
            json=data,
            headers=session_headers(request) or None,
            timeout=20,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "AI chat failed"))

    return response_data(resp)


@app.post("/receipts")
async def add_receipt(request: Request):
    data = await request.json()
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.post(
            f"{BACKEND_URL}/receipts",
            json=data,
            headers=session_headers(request) or None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to save receipt"))

    return response_data(resp)


@app.post("/receipts/batch")
async def add_receipts_batch(request: Request):
    data = await request.json()
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.post(
            f"{BACKEND_URL}/receipts/batch",
            json=data,
            headers=session_headers(request) or None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to save receipts"))

    return response_data(resp)


@app.post("/receipts/extract")
async def extract_receipt(request: Request):
    data = await request.json()
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.post(
            f"{BACKEND_URL}/receipts/extract",
            json=data,
            headers=session_headers(request) or None,
            timeout=30,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to extract receipt"))

    return response_data(resp)


@app.post("/receipts/parse-batch")
async def parse_receipts_batch(request: Request):
    data = await request.json()
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.post(
            f"{BACKEND_URL}/receipts/parse-batch",
            json=data,
            headers=session_headers(request) or None,
            timeout=40,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to parse receipts"))

    return response_data(resp)


@app.delete("/receipts/{receipt_id}")
async def delete_receipt(receipt_id: int, request: Request):
    async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
        resp = await client.delete(
            f"{BACKEND_URL}/receipts/{receipt_id}",
            headers=session_headers(request) or None,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=response_detail(resp, "Failed to delete receipt"))

    return response_data(resp)
