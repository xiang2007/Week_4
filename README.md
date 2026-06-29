# Digital AI Tax

A web app that scans receipts, extracts spending data, and provides AI-powered spending insights — all in one place.

## Features

- **Receipt Scanning** — Upload receipt images or photos; the app uses OCR to read the text
- **Amount Extraction** — Automatically detects total amounts, merchant names, dates, and spending categories from receipt text
- **PDF Conversion** — Download each receipt as a clean, formatted PDF with all extracted details
- **Spending Insights** — Visual dashboards showing spending by category, monthly trends, and top merchants
- **Receipt History** — Browse all scanned receipts in a table; delete entries you no longer need
- **Demo Data** — Ships with sample receipts so you can explore the app immediately

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Framework | [Streamlit](https://streamlit.io) — Python web UI, zero HTML/CSS/JS |
| OCR | [EasyOCR](https://github.com/JaidedAI/EasyOCR) — reads text from receipt images |
| PDF | [fpdf2](https://pyfpdf.github.io/fpdf2/) — generates formatted PDF receipts |
| Language | Python 3.14+ |
| Package manager | [uv](https://docs.astral.sh/uv/) |
| Data | Local JSON file (`receipts.json`) — no database required |

## Setup & Workflow

### Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager

### Install

```bash
# Navigate to the project directory
cd Week_4

# Install dependencies
uv add streamlit easyocr fpdf2
```

### Run

```bash
streamlit run app.py
```

The app opens at `http://localhost:8501` in your browser.

### Usage

1. **Scan Receipt** tab — Click "Upload a receipt" to select an image. The app extracts the merchant, total, date, and category. Review the data, then download as PDF.
2. **Spending Insights** tab — View your total spending, a pie chart of expenses by category, and a bar chart of monthly trends.
3. **Receipt History** tab — See all scanned receipts in a table. Delete any entry with the trash icon.

Receipts are saved locally in `receipts.json`. Uploaded images are stored in the `uploads/` directory.

## Project Structure

```
Week_4/
├── app.py              # Streamlit application (all logic in one file)
├── pyproject.toml      # Project metadata and dependencies
├── receipts.json       # Receipt data store (created on first save)
├── uploads/            # Directory for uploaded receipt images
└── README.md           # This file
```
