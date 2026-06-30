import img2pdf
import io
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import StreamingResponse

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

    return app

app = create_backend()