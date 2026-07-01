import httpx
import io

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "image"


def create_app() -> FastAPI:
    app = FastAPI(title="Frontend Server")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(name="index.html", request=request)

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
            "http://127.0.0.1:8081/process",
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
    """Send selected receipt data to the backend for tax relief summary calculation."""
    data = await request.json()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "http://127.0.0.1:8081/tax-summary",
            json=data
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Backend tax summary failed")

    return resp.json()