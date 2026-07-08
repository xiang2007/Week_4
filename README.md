# Receipt Manager - Tax Relief Dashboard

Receipt Manager is a personal tax relief dashboard for turning receipt photos into organized Malaysian tax-claim records. It helps users upload receipt images, review AI-assisted field extraction, save validated claim entries, monitor category limits, export filing evidence, and estimate tax payable from one browser-based workspace.

The project is split into two services:

- `backend/`: API, authentication, SQLite storage, OCR/Gemini integration, receipt and tax-summary logic
- `frontend/`: FastAPI frontend server that renders the landing page, app dashboard, admin page, and proxies API calls to the backend

## Product Showcase Summary

Receipt Manager is designed for individuals who keep receipts throughout the year but only organize them when tax season arrives. The product combines receipt capture, category tracking, export tools, and a lightweight tax planner so users can see what they have already claimed, what limit space remains, and which receipts still need attention.

Core value proposition:

- Replace scattered receipt photos and spreadsheets with one searchable dashboard
- Reduce manual entry through optional Gemini-powered receipt extraction
- Track Malaysian tax relief categories against configured claim limits
- Export receipt data as CSV and receipt images as a consolidated PDF filing pack
- Support demo, classroom, and prototype deployment with Docker Compose

## Features

- Account signup and login with HttpOnly cookie sessions
- Receipt image upload with manual entry, Google Vision OCR, or optional Gemini Vision extraction
- Receipt validation with a minimum amount of RM 0.01 and a configurable maximum amount
- Batch save for pending receipts
- Receipt history, filtering, CSV export, and PDF generation
- Tax claim dashboard with category limits and claimed-category summaries
- Tax planner with annual income, direct tax rebate, chargeable income, and estimated final tax payable
- AI finance helper chat powered by Gemini when configured
- Admin page for account overview, insights, and password reset
- Docker Compose setup for local development

## Tax Relief Categories

Receipt categories are configured in `backend/categories.json`. Add, remove, rename, or reorder categories there:

```json
{
  "key": "education",
  "label": "Education",
  "limit": 7000,
  "color": "#156F67",
  "keywords": ["tuition", "course", "school"]
}
```

- `key`: stable internal ID stored with receipts. Avoid changing this after receipts are saved.
- `label`: text shown in the UI.
- `limit`: claim limit number, or `null` for no limit.
- `color`: chart/UI color.
- `keywords`: OCR/category auto-selection hints. Keep keywords specific to avoid overlap.

With Docker Compose, `backend/categories.json` is mounted into the backend container and reloaded automatically on requests. Refresh the browser after editing categories.

## Tech Stack

| Area | Technology |
| --- | --- |
| Backend API | FastAPI |
| Frontend server | FastAPI + Jinja templates |
| App UI | React via CDN, Lucide icons, custom CSS |
| Storage | SQLite files under `backend/data/`, optional Supabase Storage for receipt images |
| OCR / AI | Google Cloud Vision OCR, Gemini API batch parsing/fallback, optional manual mode |
| PDF export | img2pdf |
| Runtime | Python 3.14, uv |
| Deployment | Docker / Docker Compose / Railway |

## Project Structure

```text
Week_4/
├── backend/
│   ├── src/app.py          # Backend API and business logic
│   ├── categories.json     # Editable tax category config
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
OCR_PROVIDER=google_vision
GOOGLE_VISION_API_KEY=
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash-lite
ADMIN_ACCOUNT_NAME=admin
ADMIN_PASSWORD=admin
MAX_RECEIPT_AMOUNT=100000
SUPABASE_URL=
SUPABASE_SECRET_KEY=
SUPABASE_STORAGE_BUCKET=receipt-images
SUPABASE_SIGNED_URL_SECONDS=3600
```

`OCR_PROVIDER` can be:

- `manual`: no AI OCR call; users fill receipt fields manually
- `google_vision`: use Google Cloud Vision `DOCUMENT_TEXT_DETECTION` for OCR, then parse obvious fields locally
- `gemini`: use Gemini to extract receipt fields directly from uploaded images
- `paddle`: placeholder/local OCR mode if implemented

When `OCR_PROVIDER=google_vision`, uploaded receipts are read with Google Vision first. The app stores the raw OCR text on the pending receipt and uses a local parser for obvious totals, dates, and category hints. When the user presses **Save receipt**, that receipt can be refined by Gemini before validation. When the user presses **Save all**, pending receipts with OCR text are sent to Gemini in one batch request so Gemini can auto-fill categories and refine all extracted fields at once before the backend saves the batch.

For demos without cloud keys, `OCR_PROVIDER=manual` is the safest setting because the app works without an API key.

### Optional Supabase Receipt Image Storage

By default, receipt images are stored in SQLite as data URLs so the app works locally with no cloud setup.

To store new receipt images in Supabase instead:

1. Create a Supabase project.
2. In Supabase Storage, create a private bucket named `receipt-images`.
3. Copy your project URL into `SUPABASE_URL`.
4. Copy a server-side secret key into `SUPABASE_SECRET_KEY`.
5. Restart the backend.

When Supabase is configured, the backend uploads new receipt images into the private bucket, stores the storage path in SQLite, and returns temporary signed image URLs when receipts are loaded.

For newer Supabase projects, use a key from **Settings > API Keys > Secret keys**. For older projects, the legacy **service_role** key also works if you set it as `SUPABASE_SERVICE_ROLE_KEY`.

Keep `SUPABASE_SECRET_KEY` only on the backend. Do not expose it in frontend code or commit it to git.

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
PORT=8080 HEALTHCHECK_HOST=127.0.0.1 OCR_PROVIDER=google_vision GOOGLE_VISION_API_KEY=your-google-vision-key GEMINI_API_KEY=your-gemini-key uv run uvicorn src.app:app --host 0.0.0.0 --port 8080
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
     OCR_PROVIDER=google_vision
     GOOGLE_VISION_API_KEY=your-google-vision-key
     GEMINI_API_KEY=
     GEMINI_MODEL=gemini-2.5-flash-lite
     ADMIN_ACCOUNT_NAME=admin
     ADMIN_PASSWORD=choose-a-strong-password
     MAX_RECEIPT_AMOUNT=100000
     SUPABASE_URL=https://your-project-ref.supabase.co
     SUPABASE_SECRET_KEY=your-secret-key
     SUPABASE_STORAGE_BUCKET=receipt-images
     SUPABASE_SIGNED_URL_SECONDS=3600
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
- Do not expose the Supabase secret key in frontend code.
- If a Gemini API key was exposed, rotate it before deployment.
- Login sessions are stored in SQLite and sent to the browser as HttpOnly cookies, so users stay logged in across backend restarts while the session is valid.
- SQLite files are stored inside the backend container unless a persistent volume is configured. Supabase Storage only moves receipt image files; receipt metadata remains in SQLite in this version.
- For production, use persistent storage and set strong admin credentials in environment variables.

## API Overview

Main backend endpoints:

- `POST /auth/signup`: create an account and start a login session
- `POST /auth/login`: log in and start a login session
- `GET /auth/me`: check the current login session
- `POST /auth/logout`: end the current login session
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
