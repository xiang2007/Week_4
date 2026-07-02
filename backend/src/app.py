import img2pdf
import io
import re
import secrets
import sqlite3
from hashlib import sha256
from pathlib import Path
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
                filename TEXT,
                image_data_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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


def calculate_summary(receipts: list["ReceiptItem"]):
    aggregated = {}

    for receipt in receipts:
        if receipt.amount <= 0:
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
    filename: Optional[str] = None
    imageDataUrl: Optional[str] = None


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
                SELECT id, receipt_number, amount, category_key, filename, image_data_url, created_at
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
                    "filename": row["filename"],
                    "image_data_url": row["image_data_url"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }

    @app.post("/receipts")
    async def add_receipt(
        payload: ReceiptCreate,
        authorization: Optional[str] = Header(default=None),
    ):
        account_name = require_user(authorization)
        if payload.amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be greater than zero")
        if payload.categoryKey not in TAX_RELIEF_CATEGORIES:
            raise HTTPException(status_code=400, detail="Unknown category")

        init_user_db(account_name)
        with sqlite3.connect(user_db_path(account_name)) as conn:
            cursor = conn.execute(
                """
                INSERT INTO receipts (receipt_number, amount, category_key, filename, image_data_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    payload.receiptNumber,
                    payload.amount,
                    payload.categoryKey,
                    payload.filename,
                    payload.imageDataUrl,
                ),
            )
            receipt_id = cursor.lastrowid

        return {"id": receipt_id}

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

    return app


app = create_backend()
