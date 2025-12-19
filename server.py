from cmath import rect
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import fitz  # PyMuPDF
import re
import math
import os

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ======================================================
# 1. CONFIGURATION & REGEX
# ======================================================
REGEX = r"""
\d+\.\d+\s*"? |
\d+-\d+/\d+\s*"? |
\d+/\d+\s*"? |
\d+\s*"? 
"""
VERTICAL_Y_TOLERANCE = 6   # points
MERGE_X_THRESHOLD = 10    # points


TABLE_ROW_THRESHOLD = 4
LAST_BALLOON_INDEX = 0
USED_POSITIONS = {} 
PDF_HISTORY = []

# ======================================================
# 2. ZONE & FILTER HELPERS
# ======================================================
def is_table_zone(x, y, pw, ph):
    return x > pw * 0.65 and y > ph * 0.40

def is_hard_ignore_zone(x, y, pw, ph):
    return x > pw * 0.55 and y > ph * 0.85

def has_invalid_letters(text):
    allowed = ["TYP", "REF", "R", "Ø", "DIA", "MIN", "MAX", "TO", "UNC", "UNF", "THRU"]
    t = text.upper()
    for a in allowed:
        t = t.replace(a, "")
    t = re.sub(r'[0-9\s\.\-\/"°xX]', '', t)
    return bool(re.search(r"[A-Z]", t))

def detect_surface_finish_zones(page):
    zones = []
    ph = page.rect.height
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            txt = " ".join(s["text"] for s in l["spans"]).upper()
            if "SURFACE FINISH" in txt:
                ys = [s["bbox"][1] for s in l["spans"]]
                y_min = min(ys) - 10
                y_max = y_min + ph * 0.16
                zones.append((y_min, y_max))
    return zones
def remove_vertical_close_duplicates(items):
    """
    Merge vertically aligned dimensions when left/right X is very close.
    Keeps only ONE balloon (top-most).
    """
    items = sorted(items, key=lambda i: (i["page"], i["y"]))
    result = []
    consumed = set()

    for i, a in enumerate(items):
        if i in consumed:
            continue

        for j in range(i + 1, len(items)):
            b = items[j]

            if b["page"] != a["page"]:
                break

            # 🔥 Vertical alignment (with tolerance)
            if abs(b["y"] - a["y"]) > VERTICAL_Y_TOLERANCE:
                continue

            # 🔥 Left-right closeness
            if abs(b["x"] - a["x"]) <= MERGE_X_THRESHOLD:
                consumed.add(j)

        result.append(a)

    return result



def detect_bom_columns(page):
    headers = ["ITEM", "QTY", "PART", "NO", "DESCRIPTION", "MATERIAL"]
    cols = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            txt = " ".join(s["text"] for s in l["spans"]).upper()
            if any(h in txt for h in headers):
                for s in l["spans"]:
                    x0, y0, x1, y1 = s["bbox"]
                    cols.append({"xmin": x0 - 15, "xmax": x1 + 15, "ymin": y1})
    return cols

# ======================================================
# 3. SAFE GROUPING LOGIC
# ======================================================
def group_close_dimensions(items):
    items = sorted(items, key=lambda i: (i["page"], i["y"], i["x"]))
    grouped = []
    used = set()

    Y_THRESHOLD = 3
    X_GAP_MAX = 20

    for i, a in enumerate(items):
        if i in used:
            continue

        best_j = None
        best_gap = X_GAP_MAX + 1

        for j in range(i + 1, len(items)):
            b = items[j]

            if j in used:
                continue
            if a["page"] != b["page"]:
                break
            if abs(a["y"] - b["y"]) > Y_THRESHOLD:
                continue
            if a["is_table"] or b["is_table"]:
                continue

            gap = b["x"] - a["x"]
            if 0 < gap <= X_GAP_MAX and gap < best_gap:
                best_gap = gap
                best_j = j

        if best_j is not None:
            b = items[best_j]
            grouped.append({
                "page": a["page"],
                "value": f"{a['value']}\n{b['value']}",
                "x": (a["x"] + b["x"]) / 2,
                "y": a["y"],
                "is_table": False
            })
            used.add(i)
            used.add(best_j)
        else:
            grouped.append(a)
            used.add(i)

    return grouped

def group_vertical_dimensions(items):
    items = sorted(items, key=lambda i: (i["page"], i["x"], i["y"]))
    grouped = []
    used = set()

    X_THRESHOLD = 6
    Y_GAP_MAX = 20

    for i, a in enumerate(items):
        if i in used:
            continue

        stack = [a]
        used.add(i)

        for j in range(i + 1, len(items)):
            b = items[j]

            if j in used:
                continue
            if a["page"] != b["page"]:
                break
            if abs(a["x"] - b["x"]) > X_THRESHOLD:
                continue
            if abs(b["y"] - stack[-1]["y"]) > Y_GAP_MAX:
                continue
            if a["is_table"] or b["is_table"]:
                continue

            stack.append(b)
            used.add(j)

        if len(stack) > 1:
            grouped.append({
                "page": a["page"],
                "value": "\n".join(v["value"] for v in stack),
                "x": a["x"],
                "y": stack[0]["y"],
                "is_table": False
            })
        else:
            grouped.append(a)

    return grouped

# ======================================================
# 4. EXTRACTION PIPELINE
# ======================================================
def extract_numbers(pdf_path):
    doc = fitz.open(pdf_path)
    raw_spans = []

    for pi, page in enumerate(doc):
        pw, ph = page.rect.width, page.rect.height
        bom = detect_bom_columns(page)
        sf_zones = detect_surface_finish_zones(page)

        row_hits = {}
        page_spans = []

        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue

            for l in b["lines"]:
                for s in l["spans"]:
                    x0, y0, x1, y1 = s["bbox"]
                    txt = s["text"]
                    size = s["size"]

                    if size < 7 or y1 < ph * 0.05:
                        continue
                    if is_hard_ignore_zone(x0, y0, pw, ph):
                        continue
                    if any(z[0] <= y0 <= z[1] for z in sf_zones):
                        continue
                    if not re.search(REGEX, txt, re.VERBOSE):
                        continue
                    if has_invalid_letters(txt):
                        continue

                    clean = " ".join(txt.split())

                    if re.fullmatch(r"\d{1,3}", clean):
                        continue
                    if x0 < pw * 0.15 and re.fullmatch(r"\d+", clean):
                        continue
                    if any(c["xmin"] <= x0 <= c["xmax"] and y0 > c["ymin"] for c in bom):
                        continue

                    yc = round((y0 + y1) / 2)
                    istable = is_table_zone(x0, y0, pw, ph)

                    span = {
                        "page": pi,
                        "value": clean,
                        "x": x0,
                        "y": yc,
                        "is_table": istable
                    }

                    page_spans.append(span)

                    if istable:
                        row_hits[yc] = row_hits.get(yc, 0) + 1

        dense_rows = {y for y, c in row_hits.items() if c >= TABLE_ROW_THRESHOLD}
        for s in page_spans:
            if s["is_table"] and s["y"] in dense_rows:
                continue
            raw_spans.append(s)

    processed = group_close_dimensions(raw_spans)
    vertical_grouped = group_vertical_dimensions(processed)
    final = remove_vertical_close_duplicates(vertical_grouped)
    return final

# ======================================================
# 5. BALLOONING
# ======================================================
def balloon_pdf(input_path, items, output_path="ballooned.pdf"):
    doc = fitz.open(input_path)
    global USED_POSITIONS
    USED_POSITIONS = {}

    idx = 1
    angles = [0, 30, -30, 45, -45, 60, -60, 90, -90]

    for item in items:
        page = doc[item["page"]]
        USED_POSITIONS.setdefault(item["page"], [])

        pw, ph = page.rect.width, page.rect.height
        tx, ty = item["x"], item["y"]

        OFFSET = 26
        start = idx % len(angles)
        rotated = angles[start:] + angles[:start]

        best_bx, best_by = tx + OFFSET, ty

        for ang in rotated:
            rad = math.radians(ang)
            bx = tx + OFFSET * math.cos(rad)
            by = ty + OFFSET * math.sin(rad)

            if all(math.hypot(bx - ux, by - uy) >= 22
                   for ux, uy in USED_POSITIONS[item["page"]]):
                best_bx, best_by = bx, by
                break

        best_bx = max(15, min(best_bx, pw - 15))
        best_by = max(15, min(best_by, ph - 15))

        USED_POSITIONS[item["page"]].append((best_bx, best_by))

        page.draw_line((best_bx, best_by), (tx, ty), color=(1, 0, 0), width=0.8)
        page.draw_oval(
            fitz.Rect(best_bx - 8, best_by - 8, best_bx + 8, best_by + 8),
            color=(1, 0, 0),
            width=1
        )
        page.insert_textbox(
            fitz.Rect(best_bx - 8, best_by - 8, best_bx + 8, best_by + 8),
            str(idx),
            fontsize=7,
            align=fitz.TEXT_ALIGN_CENTER,
            color=(1, 0, 0)
        )

        idx += 1

    doc.save(output_path)



def draw_single_balloon(page, tx, ty, idx, used_positions):
    pw, ph = page.rect.width, page.rect.height

    angles = [0, 30, -30, 45, -45, 60, -60, 90, -90]
    OFFSET = 26

    best_bx, best_by = tx + OFFSET, ty

    for ang in angles:
        rad = math.radians(ang)
        bx = tx + OFFSET * math.cos(rad)
        by = ty + OFFSET * math.sin(rad)


        if all(math.hypot(bx - ux, by - uy) >= 22 for ux, uy in used_positions):
            best_bx, best_by = bx, by
            break

    best_bx = max(15, min(best_bx, pw - 15))
    best_by = max(15, min(best_by, ph - 15))

    used_positions.append((best_bx, best_by))

    page.draw_line((best_bx, best_by), (tx, ty), color=(1, 0, 0), width=0.8)
    page.draw_oval(
        fitz.Rect(best_bx - 8, best_by - 8, best_bx + 8, best_by + 8),
        color=(1, 0, 0),
        width=1
    )
    page.insert_textbox(
        fitz.Rect(best_bx - 8, best_by - 8, best_bx + 8, best_by + 8),
        str(idx),
        fontsize=7,
        align=fitz.TEXT_ALIGN_CENTER,
        color=(1, 0, 0)
    )

# ===============================
# Extract tolerance
# ===============================
def extract_tolerances(pdf_path):
    doc = fitz.open(pdf_path)
    tolerance_text = ""

    for page in doc:
        text = page.get_text("text").upper()
        if "TOLERANCE" in text:
            tolerance_text = text
            break

    tolerances = {}

    # FRACTIONAL
    m = re.search(r"FRACTIONAL\s*[:\-]?\s*([\d]+)\s*/\s*([\d]+)", tolerance_text)
    if m:
        tolerances["FRACTIONAL"] = float(m.group(1)) / float(m.group(2))

    # ONE DECIMAL
    m = re.search(
        r"ONE\s+(PL\.?|PLACE)?\s*DECIMAL\s*[:\-]?\s*([\d/\.]+)",
        tolerance_text
    )
    if m:
        val = m.group(2)
        tolerances["ONE_DECIMAL"] = (
            float(val.split("/")[0]) / float(val.split("/")[1])
            if "/" in val else float(val)
        )

    # TWO DECIMAL
    m = re.search(
        r"TWO\s+(PL\.?|PLACE)?\s*DECIMAL\s*[:\-]?\s*([\d\.]+)",
        tolerance_text
    )
    if m:
        tolerances["TWO_DECIMAL"] = float(m.group(2))

    # THREE DECIMAL
    m = re.search(
        r"THREE\s+(PL\.?|PLACE)?\s*DECIMAL\s*[:\-]?\s*([\d\.]+)",
        tolerance_text
    )
    if m:
        tolerances["THREE_DECIMAL"] = float(m.group(2))

    return tolerances

# ======================================================
# 6. API ROUTES
# ======================================================
@app.post("/upload")
def upload():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    input_path = "temp.pdf"
    output_path = "ballooned.pdf"
    file.save(input_path)

    numbers = extract_numbers(input_path)
    tolerances = extract_tolerances(input_path)   # 🔥 ADD THIS
    balloon_pdf(input_path, numbers, output_path)
    save_pdf_snapshot() 

    page = fitz.open(input_path)[0]

    return jsonify({
        "status": "success",
        "numbers": numbers,
        "tolerances": tolerances,
        "balloon_pdf": "/preview-ballooned",
        "pageWidth": page.rect.width,
        "pageHeight": page.rect.height
    })

# ===============================
# ADD MANUAL BALLOON
# ===============================
@app.post("/add-manual-balloon")
def add_manual_balloon():
    try:
        # 🔥 READ PERCENT COORDINATES
        x_pct = float(request.form["x_pct"])
        y_pct = float(request.form["y_pct"])
        page_no = int(request.form.get("page", 0))
        idx = int(request.form["index"])

        if not os.path.exists("ballooned.pdf"):
            return jsonify({"error": "ballooned.pdf not found"}), 400

        doc = fitz.open("ballooned.pdf")
        page = doc[page_no]

        # 🔥 % → PDF coordinates
        x = x_pct * page.rect.width
        y = y_pct * page.rect.height

        USED_POSITIONS.setdefault(page_no, [])

        angles = [0, 30, -30, 45, -45, 60, -60, 90, -90]
        start = idx % len(angles)
        angles = angles[start:] + angles[:start]

        OFFSET = 26
        pw, ph = page.rect.width, page.rect.height

        best_bx, best_by = x + OFFSET, y

        for ang in angles:
            rad = math.radians(ang)
            bx = x + OFFSET * math.cos(rad)
            by = y + OFFSET * math.sin(rad)

            if all(math.hypot(bx - ux, by - uy) >= 22
                   for ux, uy in USED_POSITIONS[page_no]):
                best_bx, best_by = bx, by
                break

        best_bx = max(15, min(best_bx, pw - 15))
        best_by = max(15, min(best_by, ph - 15))

        USED_POSITIONS[page_no].append((best_bx, best_by))

        # 🔴 DRAW
        page.draw_line((best_bx, best_by), (x, y), color=(1, 0, 0), width=0.8)
        page.draw_oval(
            fitz.Rect(best_bx - 8, best_by - 8, best_bx + 8, best_by + 8),
            color=(1, 0, 0),
            width=1
        )
        page.insert_textbox(
            fitz.Rect(best_bx - 8, best_by - 8, best_bx + 8, best_by + 8),
            str(idx),
            fontsize=7,
            align=fitz.TEXT_ALIGN_CENTER,
            color=(1, 0, 0)
        )

        doc.save("ballooned.pdf", incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        doc.close()
        save_pdf_snapshot()

        return jsonify({"status": "ok", "index": idx})

    except Exception as e:
        print("❌ Manual balloon error:", e)
        return jsonify({"error": str(e)}), 500

@app.post("/rebuild-balloons")
def rebuild_balloons():
    data = request.json
    items = data.get("numbers", [])

    if not os.path.exists("temp.pdf"):
        return jsonify({"error": "Original PDF missing"}), 400

    balloon_pdf("temp.pdf", items)
    save_pdf_snapshot() 
    return jsonify({"status": "rebuilt"})

def save_pdf_snapshot():
    with open("ballooned.pdf", "rb") as f:
        PDF_HISTORY.append(f.read())

    # limit history (optional)
    if len(PDF_HISTORY) > 50:
        PDF_HISTORY.pop(0)

@app.post("/undo-pdf")
def undo_pdf():
    if len(PDF_HISTORY) < 2:
        return jsonify({"error": "No undo available"}), 400

    # Remove current state
    PDF_HISTORY.pop()

    # Restore previous
    with open("ballooned.pdf", "wb") as f:
        f.write(PDF_HISTORY[-1])

    return jsonify({"status": "undone"})

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