from pathlib import Path
import img2pdf

def calculate_tax_rates(income : int) -> int:
    if income >= 5001 and income <= 20000:
        return 1
    elif income >= 20001 and income <= 35000:
        return 3
    elif income >= 35001 and income <= 50000:
        return 6
    elif income >= 50001 and income <= 70000:
        return 11
    elif income >= 70001 and income <= 100000:
        return 19
    elif income >= 100001 and income <= 400000:
        return 25
    elif income >= 400001 and income <= 600000:
        return 26
    elif income >= 600001 and income <= 2000000:
        return 28
    return 30

def calculate_tax(income : int) -> float:
    if income <= 5000:
        return 0
    return (((calculate_tax_rates(income)/100)*income))

def img_to_pdf(image_path : str):
    img_path = Path(image_path).resolve()

    png_files = img_path.glob("*.png")
    jpg_files = img_path.glob("*.jpg")
    img_files = list(png_files) + list(jpg_files)

    try:
        with open("out.pdf", "wb") as f:
            f.write(img2pdf.convert(img_files))
    except Exception as e:
        print(f"Error occur {e}")



