from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz
import pytesseract
from PIL import Image
import io
import re
import os

app = Flask(__name__)
CORS(app)

# Detect OS and set tesseract path only on Windows
if os.name == "nt":
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

def extract_numbers_from_pdf(pdf_path):
    doc = fitz.open(pdf_path)
    full_text = ""

    print("Reading PDF pages...")

    for page_number in range(len(doc)):
        page = doc.load_page(page_number)
        pix = page.get_pixmap(dpi=200)
        img_data = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_data))

        text = pytesseract.image_to_string(img)
        full_text += text

        print("Page", page_number + 1, "done")

    numbers = re.findall(r"\d+", full_text)
    return numbers

@app.post("/upload")
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    pdf = request.files["file"]
    pdf.save("file.pdf")

    try:
        numbers = extract_numbers_from_pdf("file.pdf")
        return jsonify({"numbers": numbers})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
