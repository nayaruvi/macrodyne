from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import fitz  # PyMuPDF
import re
import os
import math

app = Flask(__name__)
CORS(app)

# ===============================
# REGEX FOR ENGINEERING NUMBERS
# ===============================
REGEX = r"""
\d+\.\d+ |     # decimals like 79.38
\d+/\d+ |     # fractions like 13/16
\d+            # integers
"""

# ===============================
# EXTRACT NUMBERS (SMART FILTER)
# ===============================
def extract_numbers(pdf_path):
    doc = fitz.open(pdf_path)
    extracted = []

    for page in doc:
        page_width = page.rect.width
        page_height = page.rect.height

        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue

            for line in block["lines"]:
                for span in line["spans"]:
                    x0, y0, x1, y1 = span["bbox"]
                    text = span["text"]

                    # ❌ Ignore very tiny garbage text
                    if span["size"] < 6:
                        continue

                    # ❌ Ignore top title/date area
                    if y1 < page_height * 0.05:
                        continue

                    # ❌ Ignore ONLY bottom-right notes area
                    if (
                        y0 > page_height * 0.78 and
                        x0 > page_width * 0.60
                    ):
                        continue

                    matches = re.findall(REGEX, text, re.VERBOSE)
                    for m in matches:
                        extracted.append(m)

    return extracted


# ===============================
# BALLOON NUMBERS (ANGLED, NO OVERLAP)
# ===============================
def balloon_pdf(pdf_path, numbers):
    doc = fitz.open(pdf_path)

    BALLOON_RADIUS = 8
    OFFSET = 26
    MIN_DIST = 22
    balloon_index = 1
    used_positions = []

    angle_steps = [0, 30, -30, 45, -45, 60, -60]

    for page in doc:
        page_width = page.rect.width
        page_height = page.rect.height

        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue

            for line in block["lines"]:
                for span in line["spans"]:
                    x0, y0, x1, y1 = span["bbox"]
                    text = span["text"]

                    if span["size"] < 6:
                        continue

                    # ❌ Ignore top title/date area
                    if y1 < page_height * 0.05:
                        continue

                    # ❌ Ignore ONLY bottom-right notes
                    if (
                        y0 > page_height * 0.85 and
                        x0 > page_width * 0.60
                    ):
                        continue

                    char_width = (x1 - x0) / max(len(text), 1)

                    for match in re.finditer(REGEX, text, re.VERBOSE):
                        if match.group() not in numbers:
                            continue

                        # 📍 Measurement point
                        tx = x0 + match.start() * char_width
                        ty = (y0 + y1) / 2

                        # 🎯 Angled balloon position
                        angle = math.radians(
                            angle_steps[balloon_index % len(angle_steps)]
                        )

                        bx = tx + OFFSET * math.sin(angle)
                        by = ty - OFFSET * math.cos(angle)

                        # 🔁 Avoid overlap
                        for px, py in used_positions:
                            if ((bx - px) ** 2 + (by - py) ** 2) ** 0.5 < MIN_DIST:
                                by -= MIN_DIST

                        # 🛡 Clamp inside page
                        bx = max(BALLOON_RADIUS + 5, min(bx, page_width - BALLOON_RADIUS - 5))
                        by = max(BALLOON_RADIUS + 5, min(by, page_height - BALLOON_RADIUS - 5))

                        used_positions.append((bx, by))

                        # ➖ Leader line
                        page.draw_line(
                            p1=(bx, by + BALLOON_RADIUS),
                            p2=(tx, ty),
                            color=(1, 0, 0),
                            width=0.8
                        )

                        # 🔴 Balloon circle
                        page.draw_oval(
                            fitz.Rect(
                                bx - BALLOON_RADIUS,
                                by - BALLOON_RADIUS,
                                bx + BALLOON_RADIUS,
                                by + BALLOON_RADIUS
                            ),
                            color=(1, 0, 0),
                            width=1
                        )

                        # 🔢 Index number
                        page.insert_textbox(
                            fitz.Rect(
                                bx - BALLOON_RADIUS,
                                by - BALLOON_RADIUS,
                                bx + BALLOON_RADIUS,
                                by + BALLOON_RADIUS
                            ),
                            str(balloon_index),
                            fontsize=7,
                            align=fitz.TEXT_ALIGN_CENTER,
                            color=(1, 0, 0)
                        )

                        balloon_index += 1

    doc.save("ballooned.pdf")


# ===============================
# UPLOAD ROUTE
# ===============================
@app.post("/upload")
def upload_pdf():
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        request.files["file"].save("temp.pdf")

        numbers = extract_numbers("temp.pdf")
        balloon_pdf("temp.pdf", numbers)

        return jsonify({
            "numbers": numbers,
            "balloon_pdf": "/preview-ballooned"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===============================
# PREVIEW (INLINE)
# ===============================
@app.get("/preview-ballooned")
def preview_ballooned():
    if not os.path.exists("ballooned.pdf"):
        return jsonify({"error": "Ballooned PDF not found"}), 404

    return send_file("ballooned.pdf", mimetype="application/pdf")


# ===============================
# DOWNLOAD (FORCE)
# ===============================
@app.get("/download-ballooned")
def download_ballooned():
    if not os.path.exists("ballooned.pdf"):
        return jsonify({"error": "Ballooned PDF not found"}), 404

    return send_file("ballooned.pdf", as_attachment=True)


# ===============================
# RUN SERVER
# ===============================
if __name__ == "__main__":
    app.run(port=5000, debug=True)
