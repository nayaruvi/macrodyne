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
# TABLE DETECTION PARAMETERS
# ===============================
''' TABLE_ROW_THRESHOLD = 2   # ≥ 4 numbers in same horizontal row = table
Y_TOLERANCE = 3           # pixels tolerance to treat as same row
'''
TABLE_ROW_THRESHOLD = 3     # increase threshold
Y_TOLERANCE = 4
MERGE_X_GAP = 10           # pixels to merge spans


# ===============================
# EXTRACT NUMBERS (SMART FILTER)
# ===============================
def is_table_zone(x, y, pw, ph):
    # Tight BOM / title block area
    return (
        x > pw * 0.65 and
        y > ph * 0.40
    )

def is_hard_ignore_zone(x, y, pw, ph):
    """
    Absolute ignore zone:
    - bottom-right title block corner only
    """
    return (
        x > pw * 0.60 and
        y > ph * 0.83
    )

def has_invalid_letters(text):
    """
    Allow numbers WITH common engineering suffixes.
    Reject real words / sentences.
    """

    allowed_words = [
        "TYP", "REF", "R", "Ø", "DIA",
        "UNC", "UNF", "UNEF",
        "TO", "FLAT", "MIN", "MAX"
    ]

    upper = text.upper()

    # Remove allowed engineering words
    for w in allowed_words:
        upper = upper.replace(w, "")

    # Remove allowed symbols
    upper = re.sub(r'[0-9\s\.\-\/"°xX]', '', upper)

    # If anything alphabetic remains → invalid
    return bool(re.search(r"[A-Z]", upper))


def detect_bom_columns(page):
    """
    Detect BOM / parts list column x-ranges
    based on header keywords.
    """
    headers = ["ITEM", "QTY", "PART", "NO", "DESCRIPTION"]
    bom_columns = []

    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue

        for line in block["lines"]:
            text = " ".join(span["text"] for span in line["spans"]).upper()
            if any(h in text for h in headers):
                for span in line["spans"]:
                    x0, y0, x1, y1 = span["bbox"]
                    bom_columns.append({
                        "xmin": x0 - 5,
                        "xmax": x1 + 5,
                        "ymin": y1   # everything BELOW header
                    })

    return bom_columns

def extract_numbers(pdf_path):
    doc = fitz.open(pdf_path)
    final = []

    for page_index, page in enumerate(doc):
        pw, ph = page.rect.width, page.rect.height

        spans_cache = []
        row_hits = {}

        bom_columns = detect_bom_columns(page)

        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue

            for line in block["lines"]:
                for span in line["spans"]:
                    x0, y0, x1, y1 = span["bbox"]
                    text = span["text"]

                    if span["size"] < 6:
                        continue

                    # ignore top margin
                    if y1 < ph * 0.05:
                        continue

                    upper = text.upper()
                    if any(k in upper for k in [
                        "REV", "DWG", "SHEET", "SCALE",
                        "WEIGHT", "DRAWN", "CHECKED",
                        "PROPRIETARY", "CONFIDENTIAL"
                    ]):
                        continue

                    y_center = round((y0 + y1) / 2)

                    # if span contains ANY measurement, keep FULL text
                    if re.search(REGEX, text, re.VERBOSE) and not has_invalid_letters(text):

                        clean_text = " ".join(text.split())

                        # ❌ hard ignore zone
                        if is_hard_ignore_zone(x0, y0, pw, ph):
                            continue

                        # ❌ ignore tiny text (existing balloons)
                        if span["size"] < 7:
                            continue

                        # ❌ ignore standalone integer balloon numbers
                        if re.fullmatch(r"\d{1,3}", clean_text):
                            continue

                        # ❌ ignore left-body isolated numbers (extra safety)
                        if x0 < pw * 0.15 and re.fullmatch(r"\d+", clean_text):
                            continue


                        # ❌ ignore BOM / parts table columns (ITEM / QTY / PART NO)
                        skip = False
                        for col in bom_columns:
                            if col["xmin"] <= x0 <= col["xmax"] and y0 > col["ymin"]:
                                skip = True
                                break
                        if skip:
                            continue

                        is_table_area = is_table_zone(x0, y0, pw, ph)

                        spans_cache.append({
                            "page": page_index,
                            "value": clean_text,
                            "x": x0,
                            "y": y_center,
                            "is_table_area": is_table_area
                        })


                        if is_table_area:
                            row_hits[y_center] = row_hits.get(y_center, 0) + 1
                    
                    '''if re.search(REGEX, text, re.VERBOSE):

                        tokens = extract_measurement_tokens(text)
                        tokens = list(dict.fromkeys(tokens))  # remove duplicates, keep order

                        for token in tokens:
                            if has_invalid_letters(token):
                                continue

                            clean_text = " ".join(token.split())

                            if is_hard_ignore_zone(x0, y0, pw, ph):
                                continue

                            is_table_area = is_table_zone(x0, y0, pw, ph)

                            spans_cache.append({
                                "page": page_index,
                                "value": clean_text,
                                "x": x0,
                                "y": y_center,
                                "is_table_area": is_table_area
                            })

                            if is_table_area:
                                row_hits[y_center] = row_hits.get(y_center, 0) + 1'''

        # 🔥 identify dense table rows
        table_rows = {
            y for y, count in row_hits.items()
            if count >= 4
        }

        # ✅ final filtering
        for item in spans_cache:
            if item["is_table_area"] and item["y"] in table_rows:
                continue  # ❌ real table row
            final.append(item)

    return final


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
# Extract tolerance
# ===============================
def extract_tolerances(pdf_path):
    doc = fitz.open(pdf_path)
    tolerance_text = ""

    for page in doc:
        text = page.get_text("text").upper()
        if "TOLERANCES:" in text:
            tolerance_text = text
            break

    tolerances = {}

    # FRACTIONAL
    m = re.search(r"FRACTIONAL:\s*([\d/]+)", tolerance_text)
    if m:
        num, den = m.group(1).split("/")
        tolerances["FRACTIONAL"] = float(num) / float(den)

    # ONE PL DECIMAL
    m = re.search(r"ONE PL\. DECIMAL:\s*([\d/\.]+)", tolerance_text)
    if m:
        if "/" in m.group(1):
            n, d = m.group(1).split("/")
            tolerances["ONE_DECIMAL"] = float(n) / float(d)
        else:
            tolerances["ONE_DECIMAL"] = float(m.group(1))

    # TWO PL DECIMAL
    m = re.search(r"TWO PL\. DECIMAL:\s*([\d\.]+)", tolerance_text)
    if m:
        tolerances["TWO_DECIMAL"] = float(m.group(1))

    # THREE PL DECIMAL
    m = re.search(r"THREE PL\. DECIMAL:\s*([\d\.]+)", tolerance_text)
    if m:
        tolerances["THREE_DECIMAL"] = float(m.group(1))

    return tolerances

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
        tolerances = extract_tolerances("temp.pdf")   # 🔥 ADD THIS
        balloon_pdf("temp.pdf", numbers)

        return jsonify({
            "numbers": numbers,
            "tolerances": tolerances,                 # 🔥 SEND TO FRONTEND
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