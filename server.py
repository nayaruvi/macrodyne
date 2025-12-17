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
\d+\.\d+\s*"? |
\d+-\d+/\d+\s*"? |
\d+/\d+\s*"? |
\d+\s*"? 
"""

# ===============================
# TABLE / ZONE PARAMETERS
# ===============================
TABLE_ROW_THRESHOLD = 4
Y_TOLERANCE = 4

# ===============================
# ZONE HELPERS
# ===============================
def is_table_zone(x, y, pw, ph):
    return (
        x > pw * 0.65 and
        y > ph * 0.40
    )

def is_hard_ignore_zone(x, y, pw, ph):
    # Title block / notes extreme bottom-right
    return (
        x > pw * 0.55 and
        y > ph * 0.85
    )

# ===============================
# TEXT FILTERS
# ===============================
def has_invalid_letters(text):
    allowed_words = [
        "TYP", "REF", "R", "Ø", "DIA",
        "UNC", "UNF", "UNEF",
        "MIN", "MAX", "TO"
    ]

    upper = text.upper()
    for w in allowed_words:
        upper = upper.replace(w, "")

    upper = re.sub(r'[0-9\s\.\-\/"°xX]', '', upper)
    return bool(re.search(r"[A-Z]", upper))

# ===============================
# SURFACE FINISH ZONE DETECTION
# ===============================
def detect_surface_finish_zones(page):
    """
    Detect ALL surface finish conversion zones on a page.
    Returns list of (y_min, y_max)
    """
    zones = []
    page_height = page.rect.height

    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue

        for line in block["lines"]:
            text = " ".join(span["text"] for span in line["spans"]).upper()
            if "SURFACE FINISH" in text:
                y_vals = [span["bbox"][1] for span in line["spans"]]
                y_min = min(y_vals) - 10
                y_max = y_min + (page_height * 0.16) # only 16% page height

                zones.append((y_min, y_max))

    return zones

# ===============================
# BOM DETECTION
# ===============================
def detect_bom_columns(page):
    headers = ["ITEM", "QTY", "PART", "NO", "DESCRIPTION"]
    cols = []

    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue

        for line in block["lines"]:
            text = " ".join(span["text"] for span in line["spans"]).upper()
            if any(h in text for h in headers):
                for span in line["spans"]:
                    x0, y0, x1, y1 = span["bbox"]
                    cols.append({
                        "xmin": x0 - 5,
                        "xmax": x1 + 5,
                        "ymin": y1
                    })
    return cols

# ===============================
# NUMBER EXTRACTION (FINAL)
# ===============================
def extract_numbers(pdf_path):
    doc = fitz.open(pdf_path)
    final = []

    for page_index, page in enumerate(doc):
        pw, ph = page.rect.width, page.rect.height

        spans_cache = []
        row_hits = {}

        bom_columns = detect_bom_columns(page)
        surface_finish_zones = detect_surface_finish_zones(page)

        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue

            for line in block["lines"]:
                for span in line["spans"]:
                    x0, y0, x1, y1 = span["bbox"]
                    text = span["text"]
                    size = span["size"]

                    if size < 7:
                        continue

                    if y1 < ph * 0.05:
                        continue

                    # ❌ Ignore hard title block zone
                    if is_hard_ignore_zone(x0, y0, pw, ph):
                        continue

                    # ❌ Ignore surface finish conversion zones
                    skip_sf = False
                    for y_min, y_max in surface_finish_zones:
                        if y_min <= y0 <= y_max:
                            skip_sf = True
                            break
                    if skip_sf:
                        continue

                    upper = text.upper()
                    if any(k in upper for k in [
                        "REV", "DWG", "SHEET", "SCALE",
                        "WEIGHT", "DRAWN", "CHECKED",
                        "PROPRIETARY", "CONFIDENTIAL"
                    ]):
                        continue

                    if not re.search(REGEX, text, re.VERBOSE):
                        continue

                    if has_invalid_letters(text):
                        continue

                    clean_text = " ".join(text.split())

                    if re.fullmatch(r"\d{1,3}", clean_text):
                        continue

                    if x0 < pw * 0.15 and re.fullmatch(r"\d+", clean_text):
                        continue

                    skip = False
                    for col in bom_columns:
                        if col["xmin"] <= x0 <= col["xmax"] and y0 > col["ymin"]:
                            skip = True
                            break
                    if skip:
                        continue

                    y_center = round((y0 + y1) / 2)
                    is_table = is_table_zone(x0, y0, pw, ph)

                    spans_cache.append({
                        "page": page_index,
                        "value": clean_text,
                        "x": x0,
                        "y": y_center,
                        "is_table": is_table
                    })

                    if is_table:
                        row_hits[y_center] = row_hits.get(y_center, 0) + 1

        table_rows = {y for y, c in row_hits.items() if c >= TABLE_ROW_THRESHOLD}

        for item in spans_cache:
            if item["is_table"] and item["y"] in table_rows:
                continue
            final.append(item)

    return final

# ===============================
# BALLOONING
# ===============================
def balloon_pdf(pdf_path, extracted):
    doc = fitz.open(pdf_path)

    BALLOON_RADIUS = 8
    OFFSET = 26
    MIN_DIST = 22
    used = []
    angles = [0, 30, -30, 45, -45, 60, -60]
    idx = 1

    for item in extracted:
        page = doc[item["page"]]
        pw, ph = page.rect.width, page.rect.height
        tx, ty = item["x"], item["y"]

        angle = math.radians(angles[idx % len(angles)])
        bx = tx + OFFSET * math.sin(angle)
        by = ty - OFFSET * math.cos(angle)

        for px, py in used:
            if ((bx - px)**2 + (by - py)**2)**0.5 < MIN_DIST:
                by -= MIN_DIST

        bx = max(15, min(bx, pw - 15))
        by = max(15, min(by, ph - 15))

        used.append((bx, by))

        page.draw_line((bx, by + BALLOON_RADIUS), (tx, ty), color=(1, 0, 0), width=0.8)
        page.draw_oval(fitz.Rect(bx-8, by-8, bx+8, by+8), color=(1,0,0), width=1)
        page.insert_textbox(
            fitz.Rect(bx-8, by-8, bx+8, by+8),
            str(idx),
            fontsize=7,
            align=fitz.TEXT_ALIGN_CENTER,
            color=(1,0,0)
        )
        idx += 1

    doc.save("ballooned.pdf")

# ===============================
# ROUTES
# ===============================
@app.post("/upload")
def upload_pdf():
    try:
        request.files["file"].save("temp.pdf")
        numbers = extract_numbers("temp.pdf")
        balloon_pdf("temp.pdf", numbers)
        return jsonify({
            "numbers": numbers,
            "balloon_pdf": "/preview-ballooned"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/preview-ballooned")
def preview():
    return send_file("ballooned.pdf", mimetype="application/pdf")

@app.get("/download-ballooned")
def download():
    return send_file("ballooned.pdf", as_attachment=True)

if __name__ == "__main__":
    app.run(port=5000, debug=True)
