# Receipt Manager

Receipt Manager is a FastAPI web app for uploading receipt images, saving tax-claim records, reviewing category limits, exporting receipts, and estimating Malaysian tax payable with a direct tax rebate field.

The project is split into two services:

- `backend/`: API, authentication, SQLite storage, OCR/Gemini integration, receipt and tax-summary logic
- `frontend/`: FastAPI frontend server that renders the landing page, app dashboard, admin page, and proxies API calls to the backend

## Features

- Account signup and login with bearer-token authentication
- Receipt image upload with manual entry or optional Gemini OCR extraction
- Receipt validation with a minimum amount of RM 0.01 and a configurable maximum amount
- Batch save for pending receipts
- Receipt history, filtering, CSV export, and PDF generation
- Tax claim dashboard with category limits and claimed-category summaries
- Tax planner with annual income, direct tax rebate, chargeable income, and estimated final tax payable
- AI finance helper chat powered by Gemini when configured
- Admin page for account overview, insights, and password reset
- Docker Compose setup for local development

## Tech Stack

| Area | Technology |
| --- | --- |
| Backend API | FastAPI |
| Frontend server | FastAPI + Jinja templates |
| App UI | React via CDN, Lucide icons, custom CSS |
| Storage | SQLite files under `backend/data/` |
| OCR / AI | Gemini API, optional manual mode |
| PDF export | img2pdf |
| Runtime | Python 3.14, uv |
| Deployment | Docker / Docker Compose / Railway |

## Project Structure

```text
Week_4/
├── backend/
│   ├── src/app.py          # Backend API and business logic
│   ├── Dockerfile
│   └── pyproject.toml
├── frontend/
│   ├── src/app.py          # Frontend server and API proxy
│   ├── src/templates/      # Landing, app, and admin HTML
│   ├── src/static/app.css  # App styling
│   ├── Dockerfile
│   └── pyproject.toml
├── compose.yaml
├── .env.example
└── README.md
```

## Environment Variables

Create a local `.env` file from `.env.example`:

```bash
cp .env.example .env
```

Local Docker Compose expects:

```env
BACKEND_PORT=8080
FRONTEND_PORT=8000
BACKEND_URL=http://backend:8080
HEALTHCHECK_HOST=127.0.0.1
OCR_PROVIDER=manual
GEMINI_API_KEY=
GEMINI_MODEL=gemini-1.5-flash
ADMIN_ACCOUNT_NAME=admin
ADMIN_PASSWORD=admin
MAX_RECEIPT_AMOUNT=100000
```

`OCR_PROVIDER` can be:

- `manual`: no AI OCR call; users fill receipt fields manually
- `gemini`: use Gemini to extract receipt fields from uploaded images
- `paddle`: placeholder/local OCR mode if implemented

For demos, `OCR_PROVIDER=manual` is the safest setting because the app works without an API key.

## Run Locally With Docker

Start both services:

```bash
docker compose up --build
```

Open the frontend:

```text
http://127.0.0.1:8000
```

Useful pages:

- Landing page: `http://127.0.0.1:8000/`
- User app: `http://127.0.0.1:8000/app`
- Admin page: `http://127.0.0.1:8000/admin`
- Backend docs: `http://127.0.0.1:8080/docs`

## Run Services Manually

Install dependencies for each service:

```bash
cd backend
uv sync
```

```bash
cd frontend
uv sync
```

Start backend:

```bash
cd backend
PORT=8080 HEALTHCHECK_HOST=127.0.0.1 OCR_PROVIDER=manual uv run uvicorn src.app:app --host 0.0.0.0 --port 8080
```

Start frontend in another terminal:

```bash
cd frontend
BACKEND_URL=http://127.0.0.1:8080 PORT=8000 uv run uvicorn src.app:app --host 0.0.0.0 --port 8000
```

## Admin Login

The admin account is configured through environment variables:

```env
ADMIN_ACCOUNT_NAME=admin
ADMIN_PASSWORD=admin
```

Change these values before using the app outside a demo environment.

## Deployment Notes

On Railway, deploy this as two services from the same GitHub repo:

1. Backend service
   - Root directory: `backend`
   - Builder: Dockerfile
   - Public networking port: `8080`
   - Environment:
     ```env
     PORT=8080
     HEALTHCHECK_HOST=127.0.0.1
     OCR_PROVIDER=manual
     GEMINI_API_KEY=
     GEMINI_MODEL=gemini-1.5-flash
     ADMIN_ACCOUNT_NAME=admin
     ADMIN_PASSWORD=choose-a-strong-password
     MAX_RECEIPT_AMOUNT=100000
     ```

2. Frontend service
   - Root directory: `frontend`
   - Builder: Dockerfile
   - Public networking port: `8080`
   - Environment:
     ```env
     PORT=8080
     BACKEND_URL=https://your-backend-service.up.railway.app
     ```

Use the frontend public Railway URL as the user-facing app URL.

## Important Security Notes

- Do not commit `.env` or real API keys.
- If a Gemini API key was exposed, rotate it before deployment.
- Auth tokens are stored in memory, so users may need to log in again after a backend restart.
- SQLite files are stored inside the backend container unless a persistent volume is configured.
- For production, use persistent storage and set strong admin credentials in environment variables.

## API Overview

Main backend endpoints:

- `POST /auth/signup`: create an account
- `POST /auth/login`: log in and receive a bearer token
- `GET /receipts`: load saved receipts
- `POST /receipts`: save one receipt
- `POST /receipts/batch`: save all pending receipts
- `DELETE /receipts/{receipt_id}`: delete a receipt
- `POST /receipts/extract`: extract receipt fields with configured OCR provider
- `POST /tax-summary`: calculate claimed category totals
- `GET /ai-summary`: load receipt timeline summary
- `POST /ai-chat`: ask the personal finance helper
- `POST /process`: generate a PDF from uploaded images

## Evaluation Walkthrough

For a quick demo:

1. Open `/app` and create a user account.
2. Go to `Add receipts`.
3. Upload a PNG or JPG receipt.
4. Fill in missing fields, choose a category, and save.
5. Return to the dashboard to view totals, category limits, receipt history, and timeline.
6. Open `Tax planner`, enter annual income and any tax rebate amount, then review the estimated final tax payable.
7. Try CSV/PDF export after at least one receipt is saved.
