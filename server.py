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

    for page_index, page in enumerate(doc):
        pw, ph = page.rect.width, page.rect.height

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
                    if y1 < ph * 0.05:
                        continue

                    # ❌ Ignore bottom-right notes
                    if y0 > ph * 0.85 and x0 > pw * 0.60:
                        continue

                    char_width = (x1 - x0) / max(len(text), 1)

                    for match in re.finditer(REGEX, text, re.VERBOSE):
                        extracted.append({
                            "page": page_index,
                            "value": match.group(),
                            "x": x0 + match.start() * char_width,
                            "y": (y0 + y1) / 2
                        })

    return extracted

# ===============================
# BALLOON NUMBERS (ANGLED, NO OVERLAP)
# ===============================
def balloon_pdf(pdf_path, extracted):
    doc = fitz.open(pdf_path)

    BALLOON_RADIUS = 8
    OFFSET = 26
    MIN_DIST = 22
    used_positions = []
    angle_steps = [0, 30, -30, 45, -45, 60, -60]

    balloon_index = 1

    for item in extracted:
        page = doc[item["page"]]
        pw, ph = page.rect.width, page.rect.height

        tx, ty = item["x"], item["y"]

        angle = math.radians(angle_steps[balloon_index % len(angle_steps)])
        bx = tx + OFFSET * math.sin(angle)
        by = ty - OFFSET * math.cos(angle)

        for px, py in used_positions:
            if ((bx - px)**2 + (by - py)**2)**0.5 < MIN_DIST:
                by -= MIN_DIST

        bx = max(BALLOON_RADIUS + 5, min(bx, pw - BALLOON_RADIUS - 5))
        by = max(BALLOON_RADIUS + 5, min(by, ph - BALLOON_RADIUS - 5))

        used_positions.append((bx, by))

        page.draw_line(
            p1=(bx, by + BALLOON_RADIUS),
            p2=(tx, ty),
            color=(1, 0, 0),
            width=0.8
        )

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
