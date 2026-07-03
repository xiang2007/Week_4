import img2pdf
import io
import json
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path
from datetime import date
from typing import Optional

from fastapi import FastAPI, UploadFile, File
from fastapi import Body, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel #add

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ACCOUNTS_DB = DATA_DIR / "accounts.db"
AUTH_TOKENS: dict[str, str] = {}
ADMIN_TOKENS: set[str] = set()
ADMIN_ACCOUNT_NAME = "admin"
ADMIN_PASSWORD = "admin"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
OCR_PROVIDER = os.getenv("OCR_PROVIDER", "gemini").strip().lower()
if OCR_PROVIDER not in {"manual", "gemini", "paddle"}:
    OCR_PROVIDER = "gemini"
MAX_RECEIPT_AMOUNT = float(os.getenv("MAX_RECEIPT_AMOUNT", "100000"))
MIN_RECEIPT_AMOUNT = 0.01


def hash_password(password: str) -> str:
    return sha256(password.encode("utf-8")).hexdigest()


def user_db_path(account_name: str) -> Path:
    digest = sha256(account_name.encode("utf-8")).hexdigest()
    return DATA_DIR / f"user_{digest}.db"


def init_user_db(account_name: str) -> None:
    with sqlite3.connect(user_db_path(account_name)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_number TEXT NOT NULL,
                amount REAL NOT NULL,
                category_key TEXT NOT NULL,
                title TEXT,
                receipt_date TEXT,
                filename TEXT,
                image_data_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(receipts)").fetchall()
        }
        if "title" not in columns:
            conn.execute("ALTER TABLE receipts ADD COLUMN title TEXT")
        if "receipt_date" not in columns:
            conn.execute("ALTER TABLE receipts ADD COLUMN receipt_date TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_summaries (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                summary_json TEXT NOT NULL,
                receipt_count INTEGER NOT NULL,
                receipt_signature TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def init_accounts_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ACCOUNTS_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account_name TEXT PRIMARY KEY,
                password_sha256 TEXT NOT NULL,
                user_db TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def validate_account_name(account_name: str) -> str:
    normalized = account_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", normalized):
        raise HTTPException(
            status_code=400,
            detail="Account name must be 3-40 characters using letters, numbers, _, ., or -",
        )
    return normalized


def require_user(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Login required")

    token = authorization.removeprefix("Bearer ").strip()
    account_name = AUTH_TOKENS.get(token)
    if not account_name:
        raise HTTPException(status_code=401, detail="Invalid or expired login")
    return account_name


def require_admin(authorization: Optional[str]) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Admin login required")

    token = authorization.removeprefix("Bearer ").strip()
    if token not in ADMIN_TOKENS:
        raise HTTPException(status_code=401, detail="Invalid or expired admin login")


def validate_receipt_amount(amount: float) -> None:
    if amount < MIN_RECEIPT_AMOUNT:
        raise HTTPException(status_code=400, detail="Amount must be at least RM 0.01")
    if amount > MAX_RECEIPT_AMOUNT:
        raise HTTPException(
            status_code=400,
            detail=f"Amount cannot exceed RM {MAX_RECEIPT_AMOUNT:,.2f}",
        )


def calculate_summary(receipts: list["ReceiptItem"]):
    aggregated = {}

    for receipt in receipts:
        if receipt.amount < MIN_RECEIPT_AMOUNT:
            continue

        if receipt.categoryKey not in TAX_RELIEF_CATEGORIES:
            continue

        if receipt.categoryKey not in aggregated:
            aggregated[receipt.categoryKey] = 0

        aggregated[receipt.categoryKey] += receipt.amount

    total_claimed = 0
    total_remaining = 0
    category_results = []

    for category_key, amount in aggregated.items():
        category = TAX_RELIEF_CATEGORIES[category_key]
        limit = category["limit"]

        if limit is None:
            remaining = None
            percentage = 0
            claimable_amount = amount
        else:
            remaining = max(0, limit - amount)
            total_remaining += remaining
            percentage = min((amount / limit) * 100, 100)
            claimable_amount = min(amount, limit)

        total_claimed += claimable_amount

        category_results.append({
            "category_key": category_key,
            "category_label": category["label"],
            "amount": amount,
            "claimable_amount": claimable_amount,
            "limit": limit,
            "remaining": remaining,
            "percentage": percentage,
        })

    return {
        "total_claimed": total_claimed,
        "total_remaining": total_remaining,
        "categories": category_results,
    }

class ReceiptItem(BaseModel): #added
    categoryKey: str
    amount: float


class TaxSummaryRequest(BaseModel):
    receipts: list[ReceiptItem]


class AuthRequest(BaseModel):
    accountName: str
    password: str


class AdminAuthRequest(BaseModel):
    accountName: str
    password: str


class AdminPasswordResetRequest(BaseModel):
    accountName: str
    newPassword: str


class ReceiptCreate(BaseModel):
    receiptNumber: str
    amount: float
    categoryKey: str
    title: Optional[str] = None
    receiptDate: Optional[str] = None
    filename: Optional[str] = None
    imageDataUrl: Optional[str] = None


class ReceiptBatchCreate(BaseModel):
    receipts: list[ReceiptCreate]


class ReceiptExtractRequest(BaseModel):
    imageDataUrl: str


class AiChatRequest(BaseModel):
    message: str


def empty_receipt_extract(provider: str, message: str) -> dict:
    return {
        "title": "",
        "receiptDate": "",
        "amount": 0,
        "categoryKey": "",
        "confidence": 0,
        "needsReview": True,
        "provider": provider,
        "message": message,
    }


TAX_RELIEF_CATEGORIES = {
    "education": {"label": "Education", "limit": 7000},
    "book_resources": {"label": "Book & Educational Resources", "limit": 1000},
    "it_equipment": {"label": "IT Equipment / Devices", "limit": 3000},
    "medical": {"label": "Medical Expenses", "limit": 15000},
    "selfemployed": {"label": "Self-Employed / Professional Fees", "limit": 2500},
    "insurance": {"label": "Insurance Premiums", "limit": 3000},
    "life_insurance": {"label": "Life Insurance", "limit": 3000},
    "retirement": {"label": "Retirement Contributions (CPF/EWK)", "limit": None},
    "healthcare": {"label": "Healthcare", "limit": 3000},
    "renewable_energy": {"label": "Renewable Energy Equipment", "limit": 800},
    "residential": {"label": "Residential Accommodation", "limit": 2500},
}


def normalize_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def receipt_signature(receipts: list[sqlite3.Row]) -> str:
    payload = [
        {
            "id": row["id"],
            "title": row["title"],
            "receipt_date": row["receipt_date"],
            "amount": row["amount"],
            "category_key": row["category_key"],
            "created_at": row["created_at"],
        }
        for row in receipts
    ]
    return sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def fallback_ai_summary(receipts: list[sqlite3.Row]) -> dict:
    if not receipts:
        return {
            "headline": "No receipts uploaded yet.",
            "overview": "Upload receipts to build a claim timeline and spending summary.",
            "timeline": [],
        }

    timeline = [
        {
            "date": row["receipt_date"] or row["created_at"][:10],
            "title": row["title"] or f"Receipt {row['id']}",
            "summary": f"{TAX_RELIEF_CATEGORIES[row['category_key']]['label']} claim for RM {row['amount']:.2f}.",
        }
        for row in receipts
    ]
    total = sum(row["amount"] for row in receipts)
    return {
        "headline": f"{len(receipts)} receipts summarized",
        "overview": f"Your saved receipts total RM {total:.2f}. Review limits before deciding what to claim.",
        "timeline": timeline,
    }


def generate_gemini_summary(receipts: list[sqlite3.Row]) -> dict:
    fallback = fallback_ai_summary(receipts)
    if not GEMINI_API_KEY or not receipts:
        return fallback

    compact_receipts = [
        {
            "title": row["title"] or f"Receipt {row['id']}",
            "date": row["receipt_date"] or row["created_at"][:10],
            "amount": row["amount"],
            "category": TAX_RELIEF_CATEGORIES[row["category_key"]]["label"],
        }
        for row in receipts
    ]
    prompt = (
        "Act as a concise personal finance advisor for tax receipt planning. "
        "Summarize these receipt records as compact JSON only. "
        "Return keys: headline, overview, timeline. timeline must be an array "
        "of objects with date, title, summary. Keep every timeline summary under 18 words. "
        "Give practical claim organization guidance, not investment or legal advice.\n"
        f"{json.dumps(compact_receipts, ensure_ascii=True)}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "response_mime_type": "application/json",
        },
    }).encode("utf-8")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        if not isinstance(parsed.get("timeline"), list):
            return fallback
        return {
            "headline": str(parsed.get("headline") or fallback["headline"]),
            "overview": str(parsed.get("overview") or fallback["overview"]),
            "timeline": parsed["timeline"],
        }
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError):
        return fallback


def generate_gemini_chat(message: str, receipts: list[sqlite3.Row]) -> str:
    if not GEMINI_API_KEY:
        return "Gemini API key is not set. Add GEMINI_API_KEY to .env and restart the app."

    compact_receipts = [
        {
            "title": row["title"] or f"Receipt {row['id']}",
            "date": row["receipt_date"] or row["created_at"][:10],
            "amount": row["amount"],
            "category": TAX_RELIEF_CATEGORIES[row["category_key"]]["label"],
        }
        for row in receipts
    ]
    prompt = (
        "You are a concise personal finance assistant for tax receipt planning. "
        "Use only the user's receipt records below. Do not provide legal, investment, or tax filing advice. "
        "Keep the answer under 70 words and give practical next steps. "
        "Return plain text only. Do not use Markdown, bold markers, bullets, numbered lists, or headings.\n"
        f"Receipts: {json.dumps(compact_receipts, ensure_ascii=True)}\n"
        f"User question: {message}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError):
        return "I could not reach Gemini right now. Try again in a moment."


def generate_gemini_receipt_extract(image_data_url: str) -> dict:
    fallback = empty_receipt_extract("gemini", "Gemini could not extract this receipt.")
    if not GEMINI_API_KEY:
        return empty_receipt_extract("gemini", "Gemini API key is not set.")

    match = re.fullmatch(r"data:(image/(?:png|jpeg|jpg));base64,(.+)", image_data_url)
    if not match:
        return fallback

    categories = [
        {"key": key, "label": value["label"]}
        for key, value in TAX_RELIEF_CATEGORIES.items()
    ]
    prompt = (
        "Extract receipt fields as compact JSON only. Return keys: title, receiptDate, amount, "
        "categoryKey, confidence, needsReview. receiptDate must be YYYY-MM-DD if visible. "
        "amount must be the total paid as a number. categoryKey must be one of these category keys "
        "or empty string if unsure. confidence is 0-1. needsReview true if any important field is uncertain.\n"
        f"Categories: {json.dumps(categories, ensure_ascii=True)}"
    )
    body = json.dumps({
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": match.group(1).replace("jpg", "jpeg"), "data": match.group(2)}},
            ]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "response_mime_type": "application/json",
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        parsed = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
        category_key = str(parsed.get("categoryKey") or "")
        if category_key and category_key not in TAX_RELIEF_CATEGORIES:
            category_key = ""
        return {
            "title": str(parsed.get("title") or ""),
            "receiptDate": str(parsed.get("receiptDate") or ""),
            "amount": float(parsed.get("amount") or 0),
            "categoryKey": category_key,
            "confidence": max(0, min(1, float(parsed.get("confidence") or 0))),
            "needsReview": bool(parsed.get("needsReview", True)),
            "provider": "gemini",
            "message": "",
        }
    except (urllib.error.URLError, KeyError, ValueError, json.JSONDecodeError, TimeoutError):
        return fallback


def generate_paddle_receipt_extract(image_data_url: str) -> dict:
    return empty_receipt_extract(
        "paddle",
        "PaddleOCR local extraction is selected, but the PaddleOCR runtime is not installed in this Docker image yet.",
    )


def generate_receipt_extract(image_data_url: str) -> dict:
    if OCR_PROVIDER == "manual":
        return empty_receipt_extract("manual", "OCR is disabled. Fill in the receipt fields manually.")
    if OCR_PROVIDER == "paddle":
        return generate_paddle_receipt_extract(image_data_url)
    return generate_gemini_receipt_extract(image_data_url)


def clear_ai_summary_cache(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM ai_summaries WHERE id = 1")

def create_backend() -> FastAPI:
    app = FastAPI(title="Backend server")
    init_accounts_db()

    @app.post("/process")
    async def process(files: list[UploadFile] = File(...)):
        image_bytes = []

        for file in files:
            contents = await file.read()
            image_bytes.append(contents)

        # Convert all images into a single PDF
        pdf_bytes = img2pdf.convert(image_bytes)

        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": "attachment; filename=converted.pdf"
            }
        )

    @app.post("/auth/signup")
    async def signup(payload: AuthRequest):
        account_name = validate_account_name(payload.accountName)
        if len(payload.password) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

        init_user_db(account_name)
        try:
            with sqlite3.connect(ACCOUNTS_DB) as conn:
                conn.execute(
                    "INSERT INTO accounts (account_name, password_sha256, user_db) VALUES (?, ?, ?)",
                    (account_name, hash_password(payload.password), str(user_db_path(account_name))),
                )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Account name already exists")

        token = secrets.token_urlsafe(32)
        AUTH_TOKENS[token] = account_name
        return {"token": token, "account_name": account_name}

    @app.post("/auth/login")
    async def login(payload: AuthRequest):
        account_name = validate_account_name(payload.accountName)
        with sqlite3.connect(ACCOUNTS_DB) as conn:
            row = conn.execute(
                "SELECT password_sha256 FROM accounts WHERE account_name = ?",
                (account_name,),
            ).fetchone()

        if not row or row[0] != hash_password(payload.password):
            raise HTTPException(status_code=401, detail="Invalid account name or password")

        init_user_db(account_name)
        token = secrets.token_urlsafe(32)
        AUTH_TOKENS[token] = account_name
        return {"token": token, "account_name": account_name}

    @app.post("/admin/auth/login")
    async def admin_login(payload: AdminAuthRequest):
        if not (
            secrets.compare_digest(payload.accountName, ADMIN_ACCOUNT_NAME)
            and secrets.compare_digest(payload.password, ADMIN_PASSWORD)
        ):
            raise HTTPException(status_code=401, detail="Invalid admin account name or password")

        token = secrets.token_urlsafe(32)
        ADMIN_TOKENS.add(token)
        return {"token": token, "account_name": ADMIN_ACCOUNT_NAME}

    @app.get("/admin/accounts")
    async def admin_accounts(authorization: Optional[str] = Header(default=None)):
        require_admin(authorization)
        with sqlite3.connect(ACCOUNTS_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT account_name, created_at
                FROM accounts
                ORDER BY created_at DESC, account_name
                """
            ).fetchall()

        return {
            "accounts": [
                {
                    "account_name": row["account_name"],
                    "password": "Not stored in readable form",
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }

    @app.get("/admin/insights")
    async def admin_insights(authorization: Optional[str] = Header(default=None)):
        require_admin(authorization)
        with sqlite3.connect(ACCOUNTS_DB) as conn:
            conn.row_factory = sqlite3.Row
            accounts = conn.execute(
                "SELECT account_name, created_at FROM accounts ORDER BY created_at DESC"
            ).fetchall()

        total_receipts = 0
        category_counts: dict[str, int] = {}
        newest_upload = None
        for account in accounts:
            path = user_db_path(account["account_name"])
            if not path.exists():
                continue
            init_user_db(account["account_name"])
            with sqlite3.connect(path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT category_key, created_at FROM receipts ORDER BY created_at DESC"
                ).fetchall()
            total_receipts += len(rows)
            if rows and (newest_upload is None or rows[0]["created_at"] > newest_upload):
                newest_upload = rows[0]["created_at"]
            for row in rows:
                category_counts[row["category_key"]] = category_counts.get(row["category_key"], 0) + 1

        top_categories = sorted(
            [
                {"category": TAX_RELIEF_CATEGORIES[key]["label"], "count": count}
                for key, count in category_counts.items()
                if key in TAX_RELIEF_CATEGORIES
            ],
            key=lambda item: item["count"],
            reverse=True,
        )[:5]
        return {
            "account_count": len(accounts),
            "receipt_count": total_receipts,
            "newest_upload": newest_upload,
            "top_categories": top_categories,
        }

    @app.post("/admin/accounts/reset-password")
    async def admin_reset_password(
        payload: AdminPasswordResetRequest,
        authorization: Optional[str] = Header(default=None),
    ):
        require_admin(authorization)
        account_name = validate_account_name(payload.accountName)

        if len(payload.newPassword) < 6:
            raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

        with sqlite3.connect(ACCOUNTS_DB) as conn:
            cursor = conn.execute(
                """
                UPDATE accounts
                SET password_sha256 = ?
                WHERE account_name = ?
                """,
                (hash_password(payload.newPassword), account_name),
            )

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Account not found")

        return {"updated": True, "account_name": account_name}

    @app.get("/receipts")
    async def get_receipts(authorization: Optional[str] = Header(default=None)):
        account_name = require_user(authorization)
        init_user_db(account_name)
        with sqlite3.connect(user_db_path(account_name)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, receipt_number, amount, category_key, title, receipt_date, filename, image_data_url, created_at
                FROM receipts
                ORDER BY id
                """
            ).fetchall()

        return {
            "receipts": [
                {
                    "id": row["id"],
                    "receipt_number": row["receipt_number"],
                    "amount": row["amount"],
                    "category_key": row["category_key"],
                    "title": row["title"],
                    "receipt_date": row["receipt_date"],
                    "filename": row["filename"],
                    "image_data_url": row["image_data_url"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }

    @app.get("/ocr-config")
    async def ocr_config(authorization: Optional[str] = Header(default=None)):
        require_user(authorization)
        provider_labels = {
            "manual": "Manual entry",
            "gemini": "Gemini Vision",
            "paddle": "PaddleOCR local",
        }
        return {
            "provider": OCR_PROVIDER,
            "label": provider_labels.get(OCR_PROVIDER, "Gemini Vision"),
            "enabled": OCR_PROVIDER != "manual",
        }

    @app.post("/receipts")
    async def add_receipt(
        payload: ReceiptCreate,
        authorization: Optional[str] = Header(default=None),
    ):
        account_name = require_user(authorization)
        validate_receipt_amount(payload.amount)
        if payload.categoryKey not in TAX_RELIEF_CATEGORIES:
            raise HTTPException(status_code=400, detail="Unknown category")

        init_user_db(account_name)
        with sqlite3.connect(user_db_path(account_name)) as conn:
            category_count = conn.execute(
                "SELECT COUNT(*) FROM receipts WHERE category_key = ?",
                (payload.categoryKey,),
            ).fetchone()[0]
            category_label = TAX_RELIEF_CATEGORIES[payload.categoryKey]["label"]
            title = normalize_optional_text(payload.title) or f"Receipt {category_label} {category_count + 1}"
            receipt_date = normalize_optional_text(payload.receiptDate) or date.today().isoformat()
            cursor = conn.execute(
                """
                INSERT INTO receipts (receipt_number, amount, category_key, title, receipt_date, filename, image_data_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.receiptNumber,
                    payload.amount,
                    payload.categoryKey,
                    title,
                    receipt_date,
                    payload.filename,
                    payload.imageDataUrl,
                ),
            )
            clear_ai_summary_cache(conn)
            receipt_id = cursor.lastrowid

        return {"id": receipt_id, "title": title, "receipt_date": receipt_date}

    @app.post("/receipts/batch")
    async def add_receipts_batch(
        payload: ReceiptBatchCreate,
        authorization: Optional[str] = Header(default=None),
    ):
        account_name = require_user(authorization)
        if not payload.receipts:
            raise HTTPException(status_code=400, detail="No receipts to save")
        for receipt in payload.receipts:
            validate_receipt_amount(receipt.amount)
            if receipt.categoryKey not in TAX_RELIEF_CATEGORIES:
                raise HTTPException(status_code=400, detail="Unknown category")

        init_user_db(account_name)
        saved = []
        with sqlite3.connect(user_db_path(account_name)) as conn:
            counts = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT category_key, COUNT(*) FROM receipts GROUP BY category_key"
                ).fetchall()
            }
            for receipt in payload.receipts:
                counts[receipt.categoryKey] = counts.get(receipt.categoryKey, 0) + 1
                category_label = TAX_RELIEF_CATEGORIES[receipt.categoryKey]["label"]
                title = normalize_optional_text(receipt.title) or f"Receipt {category_label} {counts[receipt.categoryKey]}"
                receipt_date = normalize_optional_text(receipt.receiptDate) or date.today().isoformat()
                cursor = conn.execute(
                    """
                    INSERT INTO receipts (receipt_number, amount, category_key, title, receipt_date, filename, image_data_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.receiptNumber,
                        receipt.amount,
                        receipt.categoryKey,
                        title,
                        receipt_date,
                        receipt.filename,
                        receipt.imageDataUrl,
                    ),
                )
                saved.append({"id": cursor.lastrowid, "title": title, "receipt_date": receipt_date})
            clear_ai_summary_cache(conn)

        return {"receipts": saved}

    @app.post("/receipts/extract")
    async def extract_receipt(
        payload: ReceiptExtractRequest,
        authorization: Optional[str] = Header(default=None),
    ):
        require_user(authorization)
        return generate_receipt_extract(payload.imageDataUrl)

    @app.delete("/receipts/{receipt_id}")
    async def delete_receipt(
        receipt_id: int,
        authorization: Optional[str] = Header(default=None),
    ):
        account_name = require_user(authorization)
        init_user_db(account_name)

        with sqlite3.connect(user_db_path(account_name)) as conn:
            cursor = conn.execute(
                "DELETE FROM receipts WHERE id = ?",
                (receipt_id,),
            )
            if cursor.rowcount:
                clear_ai_summary_cache(conn)

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Receipt not found")

        return {"deleted": True}
    
    @app.post("/tax-summary") #added
    async def tax_summary(
        payload: Optional[dict] = Body(default=None),
        authorization: Optional[str] = Header(default=None),
    ):
        if authorization:
            account_name = require_user(authorization)
            init_user_db(account_name)
            with sqlite3.connect(user_db_path(account_name)) as conn:
                rows = conn.execute(
                    "SELECT category_key, amount FROM receipts ORDER BY id"
                ).fetchall()
            return calculate_summary([
                ReceiptItem(categoryKey=row[0], amount=row[1])
                for row in rows
            ])

        if payload is None:
            raise HTTPException(status_code=400, detail="Receipt data required")

        summary_request = TaxSummaryRequest.model_validate(payload)
        return calculate_summary(summary_request.receipts)

    @app.get("/ai-summary")
    async def ai_summary(authorization: Optional[str] = Header(default=None)):
        account_name = require_user(authorization)
        init_user_db(account_name)
        with sqlite3.connect(user_db_path(account_name)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, title, receipt_date, amount, category_key, created_at
                FROM receipts
                ORDER BY COALESCE(receipt_date, DATE(created_at)), id
                """
            ).fetchall()
            signature = receipt_signature(rows)
            cached = conn.execute(
                """
                SELECT summary_json, receipt_count, receipt_signature, updated_at
                FROM ai_summaries
                WHERE id = 1
                """
            ).fetchone()
            if cached and cached["receipt_count"] == len(rows) and cached["receipt_signature"] == signature:
                summary = json.loads(cached["summary_json"])
                summary["cached"] = True
                summary["updated_at"] = cached["updated_at"]
                return summary

            summary = fallback_ai_summary(rows)
            conn.execute(
                """
                INSERT INTO ai_summaries (id, summary_json, receipt_count, receipt_signature, updated_at)
                VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    summary_json = excluded.summary_json,
                    receipt_count = excluded.receipt_count,
                    receipt_signature = excluded.receipt_signature,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (json.dumps(summary), len(rows), signature),
            )
            summary["cached"] = False
            return summary

    @app.post("/ai-chat")
    async def ai_chat(
        payload: AiChatRequest,
        authorization: Optional[str] = Header(default=None),
    ):
        account_name = require_user(authorization)
        message = payload.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="Message required")

        init_user_db(account_name)
        with sqlite3.connect(user_db_path(account_name)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, title, receipt_date, amount, category_key, created_at
                FROM receipts
                ORDER BY COALESCE(receipt_date, DATE(created_at)), id
                """
            ).fetchall()

        return {"reply": generate_gemini_chat(message, rows)}

    return app


app = create_backend()
