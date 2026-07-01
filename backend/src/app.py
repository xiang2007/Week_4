import img2pdf
import io
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel #add

class ReceiptItem(BaseModel): #added
    categoryKey: str
    amount: float


class TaxSummaryRequest(BaseModel):
    receipts: list[ReceiptItem]


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
    
    @app.post("/tax-summary") #added
    async def tax_summary(payload: TaxSummaryRequest):
        aggregated = {}

        for receipt in payload.receipts:
            if receipt.amount <= 0:
                continue

            if receipt.categoryKey not in TAX_RELIEF_CATEGORIES:
                continue

            if receipt.categoryKey not in aggregated:
                aggregated[receipt.categoryKey] = 0

            aggregated[receipt.categoryKey] += receipt.amount

        total_claimed = sum(aggregated.values())
        total_limit = 0
        category_results = []

        for category_key, amount in aggregated.items():
            category = TAX_RELIEF_CATEGORIES[category_key]
            limit = category["limit"]

            if limit is None:
                remaining = None
                percentage = 0
            else:
                total_limit += limit
                remaining = max(0, limit - amount)
                percentage = min((amount / limit) * 100, 100)

            category_results.append({
                "category_key": category_key,
                "category_label": category["label"],
                "amount": amount,
                "limit": limit,
                "remaining": remaining,
                "percentage": percentage,
            })

        total_remaining = max(0, total_limit - total_claimed)

        return {
            "total_claimed": total_claimed,
            "total_remaining": total_remaining,
            "categories": category_results,
        }

    return app


app = create_backend()