import img2pdf
import io
import json
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from hashlib import sha256
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, UploadFile, File
from fastapi import Body, Cookie, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel #add

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ACCOUNTS_DB = DATA_DIR / "accounts.db"
AUTH_TOKENS: dict[str, str] = {}
ADMIN_TOKENS: set[str] = set()
SESSION_COOKIE_NAME = "receipt_manager_session"
ADMIN_SESSION_COOKIE_NAME = "receipt_manager_admin_session"
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "7"))
ADMIN_ACCOUNT_NAME = os.getenv("ADMIN_ACCOUNT_NAME", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GOOGLE_VISION_API_KEY = os.getenv("GOOGLE_VISION_API_KEY", "").strip()
OCR_PROVIDER = os.getenv("OCR_PROVIDER", "gemini").strip().lower()
if OCR_PROVIDER not in {"manual", "gemini", "google_vision", "paddle"}:
    OCR_PROVIDER = "gemini"
MAX_RECEIPT_AMOUNT = float(os.getenv("MAX_RECEIPT_AMOUNT", "100000"))
MIN_RECEIPT_AMOUNT = 0.01
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SECRET_KEY = (
    os.getenv("SUPABASE_SECRET_KEY", "").strip()
    or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
)
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "receipt-images").strip()
SUPABASE_SIGNED_URL_SECONDS = int(os.getenv("SUPABASE_SIGNED_URL_SECONDS", "3600"))
SUPABASE_STORAGE_ENABLED = bool(SUPABASE_URL and SUPABASE_SECRET_KEY and SUPABASE_STORAGE_BUCKET)
CATEGORIES_FILE = Path(os.getenv("CATEGORIES_FILE", str(BASE_DIR / "categories.json")))


def hash_password(password: str) -> str:
    return sha256(password.encode("utf-8")).hexdigest()


def user_db_path(account_name: str) -> Path:
    digest = sha256(account_name.encode("utf-8")).hexdigest()
    return DATA_DIR / f"user_{digest}.db"


def user_storage_prefix(account_name: str) -> str:
    return sha256(account_name.encode("utf-8")).hexdigest()


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
                image_storage_path TEXT,
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
        if "image_storage_path" not in columns:
            conn.execute("ALTER TABLE receipts ADD COLUMN image_storage_path TEXT")
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                account_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT,
                provider TEXT NOT NULL,
                operation TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('success', 'warning', 'error')),
                http_status INTEGER,
                message TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def log_ai_event(
    provider: str,
    operation: str,
    status: str,
    message: str,
    account_name: Optional[str] = None,
    http_status: Optional[int] = None,
    details: Optional[dict | str] = None,
) -> None:
    if status not in {"success", "warning", "error"}:
        status = "error"
    if isinstance(details, dict):
        details_text = json.dumps(details, ensure_ascii=True)[:4000]
    elif details is None:
        details_text = None
    else:
        details_text = str(details)[:4000]

    try:
        with sqlite3.connect(ACCOUNTS_DB) as conn:
            conn.execute(
                """
                INSERT INTO ai_events (account_name, provider, operation, status, http_status, message, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_name,
                    provider,
                    operation,
                    status,
                    http_status,
                    message[:500],
                    details_text,
                ),
            )
    except sqlite3.Error:
        pass


def create_session(account_name: str, role: str) -> str:
    session_id = secrets.token_urlsafe(32)
    expires_at = (utc_now() + timedelta(days=SESSION_DAYS)).isoformat()
    with sqlite3.connect(ACCOUNTS_DB) as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_id, account_name, role, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, account_name, role, expires_at),
        )
    return session_id


def delete_session(session_id: Optional[str]) -> None:
    if not session_id:
        return
    with sqlite3.connect(ACCOUNTS_DB) as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def session_account(session_id: Optional[str], role: str) -> Optional[str]:
    if not session_id:
        return None
    with sqlite3.connect(ACCOUNTS_DB) as conn:
        row = conn.execute(
            """
            SELECT account_name, expires_at
            FROM sessions
            WHERE session_id = ? AND role = ?
            """,
            (session_id, role),
        ).fetchone()
        if not row:
            return None
        try:
            expires_at = datetime.fromisoformat(row[1])
        except ValueError:
            delete_session(session_id)
            return None
        if expires_at <= utc_now():
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            return None
        return row[0]


def set_session_cookie(response: Response, cookie_name: str, session_id: str) -> None:
    response.set_cookie(
        key=cookie_name,
        value=session_id,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=False,
    )


def validate_account_name(account_name: str) -> str:
    normalized = account_name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", normalized):
        raise HTTPException(
            status_code=400,
            detail="Account name must be 3-40 characters using letters, numbers, _, ., or -",
        )
    return normalized


def require_user(
    authorization: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    if session_id:
        account_name = session_account(session_id, "user")
        if account_name:
            return account_name

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Login required")

    token = authorization.removeprefix("Bearer ").strip()
    account_name = AUTH_TOKENS.get(token)
    if not account_name:
        raise HTTPException(status_code=401, detail="Invalid or expired login")
    return account_name


def require_admin(
    authorization: Optional[str] = None,
    session_id: Optional[str] = None,
) -> None:
    if session_id and session_account(session_id, "admin"):
        return

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
        if receipt.amount < MIN_RECEIPT_AMOUNT or receipt.amount > MAX_RECEIPT_AMOUNT:
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


class ReceiptBatchParseItem(BaseModel):
    id: int
    title: Optional[str] = None
    receiptDate: Optional[str] = None
    amount: Optional[float] = None
    categoryKey: Optional[str] = None
    rawText: Optional[str] = None


class ReceiptBatchParseRequest(BaseModel):
    receipts: list[ReceiptBatchParseItem]


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
        "rawText": "",
    }


DEFAULT_TAX_RELIEF_CATEGORIES = [
    {"key": "education", "label": "Education", "limit": 7000, "color": "#156F67", "keywords": ["tuition", "course", "school", "college", "university", "education", "training", "exam", "academy", "seminar"]},
    {"key": "book_resources", "label": "Book & Educational Resources", "limit": 1000, "color": "#C7772E", "keywords": ["book", "bookstore", "bookshop", "stationery", "textbook", "journal", "magazine", "educational resource", "popular", "mph"]},
    {"key": "it_equipment", "label": "IT Equipment / Devices", "limit": 3000, "color": "#416D94", "keywords": ["laptop", "computer", "tablet", "keyboard", "mouse", "monitor", "printer", "smartphone", "iphone", "ipad", "samsung", "huawei", "xiaomi", "lenovo", "dell", "acer", "asus", "router", "ssd", "hard drive"]},
    {"key": "medical", "label": "Medical Expenses", "limit": 15000, "color": "#8B5E34", "keywords": ["clinic", "hospital", "pharmacy", "doctor", "dental", "dentist", "optical", "optometrist", "medicine", "prescription", "guardian", "watsons", "health lane", "big pharmacy", "treatment"]},
    {"key": "selfemployed", "label": "Self-Employed / Professional Fees", "limit": 2500, "color": "#6C5A94", "keywords": ["professional fee", "consulting", "consultancy", "freelance", "self-employed", "business registration", "ssm", "accounting fee", "legal fee", "audit fee"]},
    {"key": "insurance", "label": "Insurance Premiums", "limit": 3000, "color": "#357C8A", "keywords": ["general insurance", "medical insurance", "insurance premium", "policy premium", "takaful premium"]},
    {"key": "life_insurance", "label": "Life Insurance", "limit": 3000, "color": "#8B4B55", "keywords": ["life insurance", "life assurance"]},
    {"key": "retirement", "label": "Retirement Contributions (CPF/EWK)", "limit": None, "color": "#4B6F44", "keywords": ["retirement", "cpf", "ewk", "pension", "epf", "kwsp", "prs"]},
    {"key": "healthcare", "label": "Healthcare", "limit": 3000, "color": "#9A6A2D", "keywords": ["health screening", "vaccination", "vaccine", "fitness", "gym", "sports equipment", "wellness", "physio", "physiotherapy"]},
    {"key": "renewable_energy", "label": "Renewable Energy Equipment", "limit": 800, "color": "#557A38", "keywords": ["solar", "renewable", "ev charger", "photovoltaic", "inverter", "battery storage", "green energy"]},
    {"key": "residential", "label": "Residential Accommodation", "limit": 2500, "color": "#7B6A55", "keywords": ["rent", "rental", "accommodation", "residential", "hotel", "hostel", "homestay", "airbnb", "apartment", "condo"]},
]


def normalize_category_config(items: list[dict]) -> dict[str, dict]:
    categories: dict[str, dict] = {}
    for item in items:
        key = str(item.get("key", "")).strip()
        label = str(item.get("label", "")).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not label:
            continue
        raw_limit = item.get("limit")
        limit = None if raw_limit is None else float(raw_limit)
        keywords = [
            str(keyword).strip().lower()
            for keyword in item.get("keywords", [])
            if str(keyword).strip()
        ]
        categories[key] = {
            "label": label,
            "limit": limit,
            "color": str(item.get("color") or "#416D94"),
            "keywords": keywords,
        }
    return categories


def load_tax_relief_categories() -> dict[str, dict]:
    try:
        with CATEGORIES_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, list):
            categories = normalize_category_config(data)
            if categories:
                return categories
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return normalize_category_config(DEFAULT_TAX_RELIEF_CATEGORIES)


TAX_RELIEF_CATEGORIES = load_tax_relief_categories()
CATEGORY_CONFIG_MTIME: Optional[float] = None


def refresh_tax_relief_categories() -> None:
    global CATEGORY_CONFIG_MTIME, TAX_RELIEF_CATEGORIES
    try:
        mtime = CATEGORIES_FILE.stat().st_mtime
    except OSError:
        mtime = None
    if mtime == CATEGORY_CONFIG_MTIME:
        return
    TAX_RELIEF_CATEGORIES = load_tax_relief_categories()
    CATEGORY_CONFIG_MTIME = mtime


def public_categories() -> list[dict]:
    return [
        {
            "key": key,
            "label": category["label"],
            "limit": category["limit"],
            "color": category.get("color") or "#416D94",
            "keywords": category.get("keywords", []),
        }
        for key, category in TAX_RELIEF_CATEGORIES.items()
    ]


def category_label(category_key: str) -> str:
    category = TAX_RELIEF_CATEGORIES.get(category_key)
    return category["label"] if category else "Unknown Category"


def normalize_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def parse_image_data_url(image_data_url: Optional[str]) -> Optional[tuple[str, bytes, str]]:
    if not image_data_url:
        return None

    match = re.fullmatch(r"data:(image/(?:png|jpeg|jpg|webp));base64,(.+)", image_data_url)
    if not match:
        return None

    import base64

    try:
        image_bytes = base64.b64decode(match.group(2), validate=True)
    except ValueError:
        return None

    content_type = match.group(1).replace("jpg", "jpeg")
    extension = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }[content_type]
    return content_type, image_bytes, extension


def supabase_headers(content_type: Optional[str] = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def quote_storage_path(path: str) -> str:
    return urllib.parse.quote(path.strip("/"), safe="/")


def receipt_storage_filename(title: str, receipt_date: str, extension: str) -> str:
    base = f"{title}-{receipt_date}".lower()
    base = re.sub(r"[^a-z0-9._-]+", "-", base).strip("-._")
    base = re.sub(r"-{2,}", "-", base)
    if not base:
        base = "receipt"
    return f"{base[:80]}-{secrets.token_urlsafe(6)}.{extension}"


def upload_receipt_image(
    account_name: str,
    image_data_url: Optional[str],
    title: str,
    receipt_date: str,
) -> Optional[str]:
    parsed = parse_image_data_url(image_data_url)
    if not SUPABASE_STORAGE_ENABLED or not parsed:
        return None

    content_type, image_bytes, extension = parsed
    filename = receipt_storage_filename(title, receipt_date, extension)
    storage_path = f"receipts/{user_storage_prefix(account_name)}/{filename}"
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}/{quote_storage_path(storage_path)}"
    req = urllib.request.Request(
        url,
        data=image_bytes,
        headers={
            **supabase_headers(content_type),
            "Cache-Control": "3600",
            "x-upsert": "false",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status >= 400:
                raise HTTPException(status_code=502, detail="Receipt image upload failed")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="Receipt image upload failed") from exc

    return storage_path


def signed_receipt_image_url(storage_path: Optional[str]) -> Optional[str]:
    if not SUPABASE_STORAGE_ENABLED or not storage_path:
        return None

    url = f"{SUPABASE_URL}/storage/v1/object/sign/{SUPABASE_STORAGE_BUCKET}/{quote_storage_path(storage_path)}"
    body = json.dumps({"expiresIn": SUPABASE_SIGNED_URL_SECONDS}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers=supabase_headers("application/json"),
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    signed_url = data.get("signedURL") or data.get("signedUrl")
    if not signed_url:
        return None
    if signed_url.startswith("http://") or signed_url.startswith("https://"):
        return signed_url
    if signed_url.startswith("/storage/v1/"):
        return f"{SUPABASE_URL}{signed_url}"
    return f"{SUPABASE_URL}/storage/v1{signed_url}"


def delete_receipt_image(storage_path: Optional[str]) -> None:
    if not SUPABASE_STORAGE_ENABLED or not storage_path:
        return

    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}"
    body = json.dumps({"prefixes": [storage_path]}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers=supabase_headers("application/json"),
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=12):
            return
    except (urllib.error.URLError, TimeoutError):
        return


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
            "summary": f"{category_label(row['category_key'])} claim for RM {row['amount']:.2f}.",
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
            "category": category_label(row["category_key"]),
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


def receipt_to_advisor_item(row: sqlite3.Row) -> dict:
    return {
        "title": row["title"] or f"Receipt {row['id']}",
        "date": row["receipt_date"] or row["created_at"][:10],
        "amount": round(float(row["amount"] or 0), 2),
        "category": category_label(row["category_key"]),
    }


def build_advisor_context(message: str, receipts: list[sqlite3.Row]) -> dict:
    valid_receipts = [
        row for row in receipts
        if MIN_RECEIPT_AMOUNT <= float(row["amount"] or 0) <= MAX_RECEIPT_AMOUNT
        and row["category_key"] in TAX_RELIEF_CATEGORIES
    ]
    summary = calculate_summary([
        ReceiptItem(categoryKey=row["category_key"], amount=float(row["amount"] or 0))
        for row in valid_receipts
    ])
    category_totals = [
        {
            "category": item["category_label"],
            "spent": round(float(item["amount"] or 0), 2),
            "claimable": round(float(item["claimable_amount"] or 0), 2),
            "remaining": None if item["remaining"] is None else round(float(item["remaining"]), 2),
        }
        for item in summary["categories"]
    ]
    category_totals.sort(key=lambda item: item["claimable"], reverse=True)

    query_terms = {
        term for term in re.findall(r"[A-Za-z0-9]+", message.lower())
        if len(term) >= 3
    }
    matched_receipts = []
    for row in valid_receipts:
        label = category_label(row["category_key"])
        haystack = f"{row['title'] or ''} {label} {row['receipt_date'] or ''}".lower()
        if any(term in haystack for term in query_terms):
            matched_receipts.append(row)

    recent_receipts = sorted(
        valid_receipts,
        key=lambda row: (row["receipt_date"] or row["created_at"][:10], row["id"]),
        reverse=True,
    )[:5]

    return {
        "receipt_count": len(valid_receipts),
        "total_spent": round(sum(float(row["amount"] or 0) for row in valid_receipts), 2),
        "total_claimable": round(float(summary["total_claimed"] or 0), 2),
        "category_totals": category_totals[:8],
        "matching_receipts": [receipt_to_advisor_item(row) for row in matched_receipts[:5]],
        "recent_receipts": [receipt_to_advisor_item(row) for row in recent_receipts],
    }


def fallback_advisor_reply(message: str, context: dict) -> str:
    if context["receipt_count"] == 0:
        return "No saved receipts yet. Upload and save a receipt first, then I can help review claims, limits, and categories."

    lowered = message.lower()
    if any(word in lowered for word in ("total", "claim", "claimed", "summary")):
        top_categories = ", ".join(
            f"{item['category']} RM {item['claimable']:.2f}"
            for item in context["category_totals"][:3]
        )
        return (
            f"You have {context['receipt_count']} saved receipts with RM {context['total_claimable']:.2f} "
            f"currently claimable. Top categories: {top_categories or 'none yet'}."
        )

    if "recent" in lowered or "latest" in lowered:
        latest = context["recent_receipts"][:3]
        if not latest:
            return "No recent saved receipts are available yet."
        return "Recent receipts: " + "; ".join(
            f"{item['date']} {item['title']} RM {item['amount']:.2f}"
            for item in latest
        )

    return (
        f"You have {context['receipt_count']} saved receipts and RM {context['total_claimable']:.2f} "
        "currently claimable. Ask about a category, recent receipts, or total claims for a more specific answer."
    )


def generate_gemini_chat(message: str, receipts: list[sqlite3.Row], account_name: Optional[str] = None) -> str:
    context = build_advisor_context(message, receipts)
    if not GEMINI_API_KEY:
        log_ai_event("gemini", "ai_chat", "warning", "Gemini API key is not set; fallback reply used.", account_name)
        return fallback_advisor_reply(message, context)

    prompt = (
        "You are a concise personal finance assistant for tax receipt planning. "
        "Use only the compact database context below. Do not assume access to receipts not shown. "
        "Do not provide legal, investment, or tax filing advice. "
        "Keep the answer under 70 words and give practical next steps. "
        "Return plain text only. Do not use Markdown, bold markers, bullets, numbered lists, or headings.\n"
        f"Database context: {json.dumps(context, ensure_ascii=True)}\n"
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
        reply = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        log_ai_event("gemini", "ai_chat", "success", "Gemini chat reply completed.", account_name)
        return reply
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:2000]
        message_text = f"Gemini chat HTTP {exc.code}."
        log_ai_event("gemini", "ai_chat", "error", message_text, account_name, exc.code, body)
        return "I could not reach Gemini right now. Try again in a moment."
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
        log_ai_event("gemini", "ai_chat", "error", "Gemini chat failed.", account_name, details=repr(exc))
        return "I could not reach Gemini right now. Try again in a moment."


def generate_gemini_receipt_extract(image_data_url: str, account_name: Optional[str] = None) -> dict:
    fallback = empty_receipt_extract("gemini", "Gemini could not extract this receipt.")
    if not GEMINI_API_KEY:
        log_ai_event("gemini", "receipt_extract", "error", "Gemini API key is not set.", account_name)
        return empty_receipt_extract("gemini", "Gemini API key is not set.")

    match = re.fullmatch(r"data:(image/(?:png|jpeg|jpg));base64,(.+)", image_data_url)
    if not match:
        log_ai_event("gemini", "receipt_extract", "warning", "Unsupported receipt image format.", account_name)
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
        result = {
            "title": str(parsed.get("title") or ""),
            "receiptDate": str(parsed.get("receiptDate") or ""),
            "amount": float(parsed.get("amount") or 0),
            "categoryKey": category_key,
            "confidence": max(0, min(1, float(parsed.get("confidence") or 0))),
            "needsReview": bool(parsed.get("needsReview", True)),
            "provider": "gemini",
            "message": "",
            "rawText": "",
        }
        log_ai_event("gemini", "receipt_extract", "success", "Gemini receipt extraction succeeded.", account_name)
        return result
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:2000]
        message = f"Gemini receipt extraction HTTP {exc.code}."
        log_ai_event("gemini", "receipt_extract", "error", message, account_name, exc.code, body)
        return fallback
    except (urllib.error.URLError, KeyError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        log_ai_event("gemini", "receipt_extract", "error", "Gemini receipt extraction failed.", account_name, details=repr(exc))
        return fallback


def category_from_text(text: str) -> str:
    lowered = text.lower()
    scores = {}
    for key, category in TAX_RELIEF_CATEGORIES.items():
        score = 0
        for keyword in category.get("keywords", []):
            if keyword in lowered:
                score += 2 if " " in keyword else 1
        scores[key] = score
    if not scores:
        return ""
    best_key, best_score = max(scores.items(), key=lambda item: item[1])
    return best_key if best_score else ""


def parse_receipt_text(raw_text: str, provider: str, message: str = "") -> dict:
    text = raw_text.strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    amount = 0.0
    amount_patterns = [
        r"(?:grand\s+total|net\s+total|total\s+amount|amount\s+due|total|subtotal)\D{0,20}(?:rm|myr)?\s*([0-9][0-9,]*\.\d{2})",
        r"(?:rm|myr)\s*([0-9][0-9,]*\.\d{2})",
    ]
    candidates: list[float] = []
    lowered_text = text.lower()
    for pattern in amount_patterns:
        for match in re.finditer(pattern, lowered_text, flags=re.IGNORECASE):
            try:
                candidates.append(float(match.group(1).replace(",", "")))
            except ValueError:
                pass
    if candidates:
        amount = max(candidates)

    receipt_date = ""
    date_patterns = [
        r"\b(20\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])\b",
        r"\b(0?[1-9]|[12]\d|3[01])[-/.](0?[1-9]|1[0-2])[-/.](20\d{2})\b",
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            groups = match.groups()
            if len(groups[0]) == 4:
                parsed_date = date(int(groups[0]), int(groups[1]), int(groups[2]))
            else:
                parsed_date = date(int(groups[2]), int(groups[1]), int(groups[0]))
            receipt_date = parsed_date.isoformat()
            break
        except ValueError:
            continue

    ignored_title_words = ("tax invoice", "invoice", "receipt", "cash bill")
    title = ""
    for line in lines[:6]:
        if len(line) < 3:
            continue
        if any(word in line.lower() for word in ignored_title_words):
            continue
        if re.search(r"\d{2,}", line):
            continue
        title = line[:80]
        break

    category_key = category_from_text(text)
    confidence = 0.35
    if amount:
        confidence += 0.25
    if receipt_date:
        confidence += 0.2
    if category_key:
        confidence += 0.15
    if title:
        confidence += 0.05
    confidence = min(confidence, 0.9)

    return {
        "title": title,
        "receiptDate": receipt_date,
        "amount": amount,
        "categoryKey": category_key,
        "confidence": confidence,
        "needsReview": confidence < 0.75 or not amount or not category_key,
        "provider": provider,
        "message": message,
        "rawText": text,
    }


def generate_google_vision_receipt_extract(image_data_url: str, account_name: Optional[str] = None) -> dict:
    if not GOOGLE_VISION_API_KEY:
        log_ai_event("google_vision", "ocr_extract", "error", "Google Vision API key is not set.", account_name)
        return empty_receipt_extract("google_vision", "Google Vision API key is not set.")

    match = re.fullmatch(r"data:image/(?:png|jpeg|jpg|webp);base64,(.+)", image_data_url)
    if not match:
        log_ai_event("google_vision", "ocr_extract", "warning", "Unsupported receipt image format.", account_name)
        return empty_receipt_extract("google_vision", "Unsupported receipt image format.")

    body = json.dumps({
        "requests": [{
            "image": {"content": match.group(1)},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
        }]
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        response = data["responses"][0]
        if response.get("error"):
            error = response["error"]
            message = error.get("message", "Google Vision OCR failed.")
            log_ai_event(
                "google_vision",
                "ocr_extract",
                "error",
                message,
                account_name,
                error.get("code"),
                error,
            )
            return empty_receipt_extract("google_vision", message)
        raw_text = response.get("fullTextAnnotation", {}).get("text", "")
        if not raw_text and response.get("textAnnotations"):
            raw_text = response["textAnnotations"][0].get("description", "")
        if not raw_text.strip():
            log_ai_event("google_vision", "ocr_extract", "warning", "No text was detected.", account_name)
            return empty_receipt_extract("google_vision", "No text was detected. Fill in the fields manually.")
        result = parse_receipt_text(raw_text, "google_vision")
        log_ai_event(
            "google_vision",
            "ocr_extract",
            "success" if not result["needsReview"] else "warning",
            "Google Vision OCR completed.",
            account_name,
            details={"chars": len(raw_text), "needsReview": result["needsReview"], "confidence": result["confidence"]},
        )
        return result
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:2000]
        message = f"Google Vision OCR HTTP {exc.code}."
        log_ai_event("google_vision", "ocr_extract", "error", message, account_name, exc.code, body)
        return empty_receipt_extract("google_vision", "Google Vision OCR failed. Fill in the fields manually.")
    except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
        log_ai_event("google_vision", "ocr_extract", "error", "Google Vision OCR failed.", account_name, details=repr(exc))
        return empty_receipt_extract("google_vision", "Google Vision OCR failed. Fill in the fields manually.")


def parse_gemini_receipt_batch(receipts: list[ReceiptBatchParseItem], account_name: Optional[str] = None) -> dict:
    if not GEMINI_API_KEY:
        log_ai_event("gemini", "batch_parse", "error", "Gemini API key is not set.", account_name)
        return {"receipts": [], "message": "Gemini API key is not set."}

    categories = [
        {"key": key, "label": value["label"]}
        for key, value in TAX_RELIEF_CATEGORIES.items()
    ]
    receipt_context = [
        {
            "id": item.id,
            "existing": {
                "title": item.title or "",
                "receiptDate": item.receiptDate or "",
                "amount": item.amount or 0,
                "categoryKey": item.categoryKey or "",
            },
            "ocrText": (item.rawText or "")[:5000],
        }
        for item in receipts
        if (item.rawText or "").strip()
    ]
    if not receipt_context:
        log_ai_event("gemini", "batch_parse", "warning", "No OCR text is available for Gemini batch parsing.", account_name)
        return {"receipts": [], "message": "No OCR text is available for Gemini batch parsing."}

    prompt = (
        "You are parsing OCR text from multiple Malaysian receipt images. "
        "Return compact JSON only with key receipts. receipts must be an array with one item per input id. "
        "Each item must have: id, title, receiptDate, amount, categoryKey, confidence, needsReview. "
        "Use existing fields when OCR text does not improve them. receiptDate must be YYYY-MM-DD if visible. "
        "amount must be the final total paid. categoryKey must be one of the allowed keys or empty string. "
        "Set needsReview true if amount/category/date is uncertain.\n"
        f"Allowed categories: {json.dumps(categories, ensure_ascii=True)}\n"
        f"Receipts: {json.dumps(receipt_context, ensure_ascii=True)}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        parsed = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
        parsed_receipts = parsed.get("receipts", []) if isinstance(parsed, dict) else []
        results = []
        for item in parsed_receipts:
            category_key = str(item.get("categoryKey") or "")
            if category_key and category_key not in TAX_RELIEF_CATEGORIES:
                category_key = ""
            try:
                receipt_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            results.append({
                "id": receipt_id,
                "title": str(item.get("title") or ""),
                "receiptDate": str(item.get("receiptDate") or ""),
                "amount": float(item.get("amount") or 0),
                "categoryKey": category_key,
                "confidence": max(0, min(1, float(item.get("confidence") or 0))),
                "needsReview": bool(item.get("needsReview", True)),
                "provider": "gemini_batch",
                "message": "",
            })
        log_ai_event(
            "gemini",
            "batch_parse",
            "success",
            "Gemini batch parsing completed.",
            account_name,
            details={"inputReceipts": len(receipt_context), "parsedReceipts": len(results)},
        )
        return {"receipts": results, "message": ""}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:2000]
        message = f"Gemini batch parsing HTTP {exc.code}."
        log_ai_event("gemini", "batch_parse", "error", message, account_name, exc.code, body)
        return {"receipts": [], "message": "Gemini batch parsing failed. Review receipts manually."}
    except (urllib.error.URLError, KeyError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        log_ai_event("gemini", "batch_parse", "error", "Gemini batch parsing failed.", account_name, details=repr(exc))
        return {"receipts": [], "message": "Gemini batch parsing failed. Review receipts manually."}


def generate_paddle_receipt_extract(image_data_url: str) -> dict:
    return empty_receipt_extract(
        "paddle",
        "PaddleOCR local extraction is selected, but the PaddleOCR runtime is not installed in this Docker image yet.",
    )


def generate_receipt_extract(image_data_url: str, account_name: Optional[str] = None) -> dict:
    if OCR_PROVIDER == "manual":
        return empty_receipt_extract("manual", "OCR is disabled. Fill in the receipt fields manually.")
    if OCR_PROVIDER == "google_vision":
        return generate_google_vision_receipt_extract(image_data_url, account_name)
    if OCR_PROVIDER == "paddle":
        return generate_paddle_receipt_extract(image_data_url)
    return generate_gemini_receipt_extract(image_data_url, account_name)


def clear_ai_summary_cache(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM ai_summaries WHERE id = 1")

def create_backend() -> FastAPI:
    app = FastAPI(title="Backend server")
    init_accounts_db()

    @app.middleware("http")
    async def refresh_category_config(request, call_next):
        refresh_tax_relief_categories()
        return await call_next(request)

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
    async def signup(payload: AuthRequest, response: Response):
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

        session_id = create_session(account_name, "user")
        set_session_cookie(response, SESSION_COOKIE_NAME, session_id)
        return {"account_name": account_name}

    @app.post("/auth/login")
    async def login(payload: AuthRequest, response: Response):
        account_name = validate_account_name(payload.accountName)
        with sqlite3.connect(ACCOUNTS_DB) as conn:
            row = conn.execute(
                "SELECT password_sha256 FROM accounts WHERE account_name = ?",
                (account_name,),
            ).fetchone()

        if not row or row[0] != hash_password(payload.password):
            raise HTTPException(status_code=401, detail="Invalid account name or password")

        init_user_db(account_name)
        session_id = create_session(account_name, "user")
        set_session_cookie(response, SESSION_COOKIE_NAME, session_id)
        return {"account_name": account_name}

    @app.get("/auth/me")
    async def auth_me(
        receipt_manager_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        account_name = session_account(receipt_manager_session, "user")
        if not account_name:
            raise HTTPException(status_code=401, detail="Login required")
        return {"account_name": account_name}

    @app.post("/auth/logout")
    async def logout(
        response: Response,
        receipt_manager_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        delete_session(receipt_manager_session)
        response.delete_cookie(SESSION_COOKIE_NAME, samesite="lax")
        return {"logged_out": True}

    @app.post("/admin/auth/login")
    async def admin_login(payload: AdminAuthRequest, response: Response):
        if not (
            secrets.compare_digest(payload.accountName, ADMIN_ACCOUNT_NAME)
            and secrets.compare_digest(payload.password, ADMIN_PASSWORD)
        ):
            raise HTTPException(status_code=401, detail="Invalid admin account name or password")

        session_id = create_session(ADMIN_ACCOUNT_NAME, "admin")
        set_session_cookie(response, ADMIN_SESSION_COOKIE_NAME, session_id)
        return {"account_name": ADMIN_ACCOUNT_NAME}

    @app.get("/admin/auth/me")
    async def admin_auth_me(
        receipt_manager_admin_session: Optional[str] = Cookie(default=None, alias=ADMIN_SESSION_COOKIE_NAME),
    ):
        if not session_account(receipt_manager_admin_session, "admin"):
            raise HTTPException(status_code=401, detail="Admin login required")
        return {"account_name": ADMIN_ACCOUNT_NAME}

    @app.post("/admin/auth/logout")
    async def admin_logout(
        response: Response,
        receipt_manager_admin_session: Optional[str] = Cookie(default=None, alias=ADMIN_SESSION_COOKIE_NAME),
    ):
        delete_session(receipt_manager_admin_session)
        response.delete_cookie(ADMIN_SESSION_COOKIE_NAME, samesite="lax")
        return {"logged_out": True}

    @app.get("/admin/accounts")
    async def admin_accounts(
        authorization: Optional[str] = Header(default=None),
        receipt_manager_admin_session: Optional[str] = Cookie(default=None, alias=ADMIN_SESSION_COOKIE_NAME),
    ):
        require_admin(authorization, receipt_manager_admin_session)
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
    async def admin_insights(
        authorization: Optional[str] = Header(default=None),
        receipt_manager_admin_session: Optional[str] = Cookie(default=None, alias=ADMIN_SESSION_COOKIE_NAME),
    ):
        require_admin(authorization, receipt_manager_admin_session)
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
                {"category": category_label(key), "count": count}
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

    @app.get("/admin/ai-events")
    async def admin_ai_events(
        authorization: Optional[str] = Header(default=None),
        receipt_manager_admin_session: Optional[str] = Cookie(default=None, alias=ADMIN_SESSION_COOKIE_NAME),
    ):
        require_admin(authorization, receipt_manager_admin_session)
        with sqlite3.connect(ACCOUNTS_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, account_name, provider, operation, status, http_status, message, details, created_at
                FROM ai_events
                ORDER BY id DESC
                LIMIT 80
                """
            ).fetchall()

        return {
            "events": [
                {
                    "id": row["id"],
                    "account_name": row["account_name"],
                    "provider": row["provider"],
                    "operation": row["operation"],
                    "status": row["status"],
                    "http_status": row["http_status"],
                    "message": row["message"],
                    "details": row["details"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }

    @app.post("/admin/accounts/reset-password")
    async def admin_reset_password(
        payload: AdminPasswordResetRequest,
        authorization: Optional[str] = Header(default=None),
        receipt_manager_admin_session: Optional[str] = Cookie(default=None, alias=ADMIN_SESSION_COOKIE_NAME),
    ):
        require_admin(authorization, receipt_manager_admin_session)
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
    async def get_receipts(
        authorization: Optional[str] = Header(default=None),
        receipt_manager_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        account_name = require_user(authorization, receipt_manager_session)
        init_user_db(account_name)
        with sqlite3.connect(user_db_path(account_name)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, receipt_number, amount, category_key, title, receipt_date, filename, image_data_url, image_storage_path, created_at
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
                    "image_data_url": signed_receipt_image_url(row["image_storage_path"]) or row["image_data_url"],
                    "image_storage_path": row["image_storage_path"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }

    @app.get("/categories")
    async def categories():
        return {"categories": public_categories()}

    @app.get("/ocr-config")
    async def ocr_config(
        authorization: Optional[str] = Header(default=None),
        receipt_manager_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        require_user(authorization, receipt_manager_session)
        provider_labels = {
            "manual": "Manual entry",
            "gemini": "Gemini Vision",
            "google_vision": "Google Vision OCR",
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
        receipt_manager_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        account_name = require_user(authorization, receipt_manager_session)
        validate_receipt_amount(payload.amount)
        if payload.categoryKey not in TAX_RELIEF_CATEGORIES:
            raise HTTPException(status_code=400, detail="Unknown category")

        init_user_db(account_name)
        with sqlite3.connect(user_db_path(account_name)) as conn:
            category_count = conn.execute(
                "SELECT COUNT(*) FROM receipts WHERE category_key = ?",
                (payload.categoryKey,),
            ).fetchone()[0]
            label = category_label(payload.categoryKey)
            title = normalize_optional_text(payload.title) or f"Receipt {label} {category_count + 1}"
            receipt_date = normalize_optional_text(payload.receiptDate) or date.today().isoformat()
            image_storage_path = upload_receipt_image(
                account_name,
                payload.imageDataUrl,
                title,
                receipt_date,
            )
            image_data_url = None if image_storage_path else payload.imageDataUrl
            cursor = conn.execute(
                """
                INSERT INTO receipts (receipt_number, amount, category_key, title, receipt_date, filename, image_data_url, image_storage_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.receiptNumber,
                    payload.amount,
                    payload.categoryKey,
                    title,
                    receipt_date,
                    payload.filename,
                    image_data_url,
                    image_storage_path,
                ),
            )
            clear_ai_summary_cache(conn)
            receipt_id = cursor.lastrowid

        return {"id": receipt_id, "title": title, "receipt_date": receipt_date}

    @app.post("/receipts/batch")
    async def add_receipts_batch(
        payload: ReceiptBatchCreate,
        authorization: Optional[str] = Header(default=None),
        receipt_manager_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        account_name = require_user(authorization, receipt_manager_session)
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
                label = category_label(receipt.categoryKey)
                title = normalize_optional_text(receipt.title) or f"Receipt {label} {counts[receipt.categoryKey]}"
                receipt_date = normalize_optional_text(receipt.receiptDate) or date.today().isoformat()
                image_storage_path = upload_receipt_image(
                    account_name,
                    receipt.imageDataUrl,
                    title,
                    receipt_date,
                )
                image_data_url = None if image_storage_path else receipt.imageDataUrl
                cursor = conn.execute(
                    """
                    INSERT INTO receipts (receipt_number, amount, category_key, title, receipt_date, filename, image_data_url, image_storage_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.receiptNumber,
                        receipt.amount,
                        receipt.categoryKey,
                        title,
                        receipt_date,
                        receipt.filename,
                        image_data_url,
                        image_storage_path,
                    ),
                )
                saved.append({"id": cursor.lastrowid, "title": title, "receipt_date": receipt_date})
            clear_ai_summary_cache(conn)

        return {"receipts": saved}

    @app.post("/receipts/extract")
    async def extract_receipt(
        payload: ReceiptExtractRequest,
        authorization: Optional[str] = Header(default=None),
        receipt_manager_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        account_name = require_user(authorization, receipt_manager_session)
        return generate_receipt_extract(payload.imageDataUrl, account_name)

    @app.post("/receipts/parse-batch")
    async def parse_receipts_batch(
        payload: ReceiptBatchParseRequest,
        authorization: Optional[str] = Header(default=None),
        receipt_manager_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        account_name = require_user(authorization, receipt_manager_session)
        return parse_gemini_receipt_batch(payload.receipts, account_name)

    @app.delete("/receipts/{receipt_id}")
    async def delete_receipt(
        receipt_id: int,
        authorization: Optional[str] = Header(default=None),
        receipt_manager_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        account_name = require_user(authorization, receipt_manager_session)
        init_user_db(account_name)

        with sqlite3.connect(user_db_path(account_name)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT image_storage_path FROM receipts WHERE id = ?",
                (receipt_id,),
            ).fetchone()
            cursor = conn.execute(
                "DELETE FROM receipts WHERE id = ?",
                (receipt_id,),
            )
            if cursor.rowcount:
                clear_ai_summary_cache(conn)

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Receipt not found")

        delete_receipt_image(row["image_storage_path"] if row else None)
        return {"deleted": True}
    
    @app.post("/tax-summary") #added
    async def tax_summary(
        payload: Optional[dict] = Body(default=None),
        authorization: Optional[str] = Header(default=None),
        receipt_manager_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        if authorization or receipt_manager_session:
            account_name = require_user(authorization, receipt_manager_session)
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
    async def ai_summary(
        authorization: Optional[str] = Header(default=None),
        receipt_manager_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        account_name = require_user(authorization, receipt_manager_session)
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
        receipt_manager_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    ):
        account_name = require_user(authorization, receipt_manager_session)
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

        return {"reply": generate_gemini_chat(message, rows, account_name)}

    return app


app = create_backend()
