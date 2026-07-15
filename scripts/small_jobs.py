# -*- coding: utf-8 -*-
"""
Small Jobs - Receipt Assignment Web App
Run:  python scripts/receipt_app.py
Open: http://localhost:5001
"""

import json, os, re, uuid, datetime
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

BASE_DIR  = Path(__file__).resolve().parent.parent
UPLOADS   = BASE_DIR / "data" / "receipts" / "pending"
EXPENSES  = BASE_DIR / "data" / "expenses"
HOURS     = BASE_DIR / "data" / "hours"
CUSTOMERS = BASE_DIR / "customers.json"
TEMPLATES = Path(__file__).resolve().parent / "templates"

for d in (UPLOADS, EXPENSES, HOURS):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(TEMPLATES))
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB max upload

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".pdf", ".heic", ".webp"}

# ── Tesseract setup ──────────────────────────────────────────────────────────
_TESS_WIN_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"C:\Users\Dandy admin\AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
]

def _init_tesseract():
    try:
        import pytesseract
        from PIL import Image  # noqa — just verify Pillow is present
        for p in _TESS_WIN_PATHS:
            if Path(p).exists():
                pytesseract.pytesseract.tesseract_cmd = p
                break
        pytesseract.get_tesseract_version()
        print("  Tesseract OCR ready.")
        return True
    except Exception as e:
        print(f"  Tesseract not ready ({e}).")
        print("  1. Install Tesseract: https://github.com/UB-Mannheim/tesseract/wiki")
        print("  2. pip install pytesseract Pillow")
        return False

_OCR_READY = _init_tesseract()


SKIP = {"total", "subtotal", "tax", "change", "cash", "credit", "debit",
        "card", "balance", "payment", "thank", "welcome", "cashier",
        "terminal", "transaction", "approved", "visa", "mastercard",
        "reward", "rewards", "rebate", "bc amt", "bk card"}

VENDOR_SKIP = {"welcome", "thank", "thank you", "have", "please",
               "come again", "receipt", "customer copy", "merchant copy"}



def parse_items(lines):
    items = []

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        # Find all price-looking numbers anywhere in the line.
        # Handles "$22.99", "6 , 99" (spaced), "19.14" (no $ prefix).
        price_matches = list(re.finditer(
            r'\$?\s*(\d{1,4})\s*[.,]\s*(\d{2})(?!\d)', line))
        if not price_matches:
            continue

        # Use the LAST price on the line (most likely the actual price, not a code)
        m = price_matches[-1]

        # Skip negative amounts — they're returns/credits/reward deductions
        prefix_text = line[:m.start()].rstrip()
        if prefix_text.endswith('-') or prefix_text.endswith('$-'):
            continue

        try:
            amt = float(f"{m.group(1)}.{m.group(2)}")
        except ValueError:
            continue

        if amt <= 0 or amt >= 2000:
            continue

        # Description = everything before the price match
        desc = prefix_text.rstrip('$').strip()

        # If description is mostly codes/symbols (few real letters), use previous line
        if len(re.sub(r'[^a-zA-Z]', '', desc)) < 3 and i > 0:
            prev = lines[i - 1].strip()
            if not re.search(r'\d+[.,]\d{2}', prev):  # prev must not have its own price
                desc = prev

        if not desc:
            continue

        lower = desc.lower()
        if any(w in lower for w in SKIP):
            continue

        items.append({"description": desc, "amount": amt, "customer_key": ""})

    return items


def guess_meta(lines):
    vendor, date = "", ""
    for line in lines[:15]:
        line  = line.strip()
        lower = line.lower()
        if not vendor and len(line) >= 3 and not re.search(r"\d{2,}", line):
            if not any(lower == w or lower.startswith(w) for w in VENDOR_SKIP):
                vendor = line
        if not date:
            m = re.search(r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", line)
            if m:
                raw = m.group(1)
                # Try 2-digit year first so "6/29/26" → 2026, not year 26
                for fmt in ("%m/%d/%y", "%m-%d-%y", "%m/%d/%Y", "%m-%d-%Y"):
                    try:
                        date = datetime.datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        pass
                if not date:
                    date = raw
    return vendor, date


def ocr_file(path: Path):
    if not _OCR_READY:
        return [], []

    import pytesseract
    from PIL import Image, ImageEnhance, ImageFilter

    img_path = path
    tmp_png  = None

    if path.suffix.lower() == ".pdf":
        try:
            import fitz
            doc     = fitz.open(str(path))
            pix     = doc[0].get_pixmap(matrix=fitz.Matrix(3.5, 3.5))
            tmp_png = path.with_suffix(".ocr_tmp.png")
            pix.save(str(tmp_png))
            img_path = tmp_png
        except Exception as e:
            print(f"  PDF render error: {e}")
            return [], []

    try:
        img = Image.open(str(img_path)).convert("L")        # grayscale
        img = ImageEnhance.Contrast(img).enhance(2.0)       # boost contrast
        img = img.filter(ImageFilter.SHARPEN)               # sharpen edges

        text  = pytesseract.image_to_string(img, config="--psm 6 --oem 3")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
    except Exception as e:
        print(f"  OCR error: {e}")
        lines = []
    finally:
        if tmp_png and tmp_png.exists():
            tmp_png.unlink()

    return lines, parse_items(lines)


# ── Customer helpers ─────────────────────────────────────────────────────────

def _save_customers(db):
    tmp = CUSTOMERS.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(CUSTOMERS))


def match_customer_key(job_name, db):
    """Multi-step fuzzy match of a job name string to a customer key."""
    job_stripped = job_name.strip()
    job_lower    = job_stripped.lower()

    # 1. Exact alias match (case-insensitive)
    for key, cust in db.items():
        for alias in cust.get("aliases", []):
            if alias.strip().lower() == job_lower:
                return key

    # 2. Exact bill_to_name match
    for key, cust in db.items():
        if cust.get("bill_to_name", "").strip().lower() == job_lower:
            return key

    # 3. Exact property_label match
    for key, cust in db.items():
        if cust.get("property_label", "").strip().lower() == job_lower:
            return key

    # 4. Substring alias match (aliases >= 3 chars, sorted by length desc)
    candidates = []
    for key, cust in db.items():
        for alias in cust.get("aliases", []):
            a = alias.strip().lower()
            if len(a) >= 3 and (a in job_lower or job_lower in a):
                candidates.append((len(a), key))
    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    return ""


# ── Hours CSV parser ─────────────────────────────────────────────────────────

def parse_hours_csv(content_bytes):
    """Parse Easy Hours CSV export. Returns (period, entries)."""
    text = None
    for enc in ("utf-8-sig", "utf-8", "utf-16", "latin-1"):
        try:
            text = content_bytes.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        return "", []

    import csv, io
    reader = csv.reader(io.StringIO(text))
    rows   = list(reader)

    period  = ""
    entries = []

    # Find the period from the first non-empty row that looks like a date range header
    for row in rows:
        if not row or not row[0].strip():
            continue
        m = re.search(r'([A-Za-z]+)\s+\d+,\s+(\d{4})', row[0])
        if m:
            month_name = m.group(1)
            year       = m.group(2)
            try:
                dt = datetime.datetime.strptime(f"{month_name} 1 {year}", "%B 1 %Y")
                period = f"{year}_{dt.month:02d}"
            except ValueError:
                period = f"{year}_00"
            break

    for row in rows:
        if len(row) < 6:
            continue
        # Skip header row
        if row[0].strip().lower() == "date":
            continue
        # Skip blank rows
        if not row[0].strip():
            continue
        # Skip summary rows
        if row[1].strip().lower() == "total":
            continue

        date_raw   = row[0].strip().strip('"')
        job_raw    = row[1].strip().strip('"')
        in_time    = row[2].strip().strip('"') if len(row) > 2 else ""
        out_time   = row[3].strip().strip('"') if len(row) > 3 else ""
        hours_raw  = row[5].strip().strip('"') if len(row) > 5 else ""
        memo       = row[7].strip().strip('"') if len(row) > 7 else ""

        # Parse date
        date_str = ""
        try:
            dt = datetime.datetime.strptime(date_raw, "%m/%d/%y")
            date_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            date_str = date_raw

        # Parse hours — strip trailing 'h'
        hours = 0.0
        try:
            hours = float(hours_raw.rstrip("h").strip())
        except ValueError:
            pass

        if not date_str and not job_raw:
            continue

        entries.append({
            "date":           date_str,
            "job_raw":        job_raw,
            "customer_key":   "",
            "property_label": "",
            "in_time":        in_time,
            "out_time":       out_time,
            "hours":          hours,
            "memo":           memo,
        })

    return period, entries


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("small_jobs.html")


@app.route("/customers")
def api_customers():
    with open(CUSTOMERS, encoding="utf-8") as f:
        db = json.load(f)
    TYPE_ORDER = {"rental_advisor": 0, "hoa": 1, "multi_property": 2, "individual": 3}
    out = sorted(
        [{"key": k, "label": c.get("property_label", k), "type": c.get("customer_type", "individual")}
         for k, c in db.items()],
        key=lambda x: (TYPE_ORDER.get(x["type"], 9), x["label"].lower()),
    )
    return jsonify(out)


@app.route("/customers/full")
def api_customers_full():
    with open(CUSTOMERS, encoding="utf-8") as f:
        db = json.load(f)
    TYPE_ORDER = {"rental_advisor": 0, "hoa": 1, "multi_property": 2, "individual": 3}
    out = []
    for k, c in db.items():
        entry = dict(c)
        entry["key"] = k
        out.append(entry)
    out.sort(key=lambda x: (
        TYPE_ORDER.get(x.get("customer_type", "individual"), 9),
        x.get("property_label", "").lower()
    ))
    return jsonify(out)


@app.route("/customers/<key>", methods=["GET"])
def api_customer_get(key):
    with open(CUSTOMERS, encoding="utf-8") as f:
        db = json.load(f)
    if key not in db:
        return jsonify({"error": "Not found"}), 404
    entry = dict(db[key])
    entry["key"] = key
    return jsonify(entry)


@app.route("/customers", methods=["POST"])
def api_customer_create():
    body  = request.get_json()
    label = (body.get("property_label") or "").strip()
    if not label:
        return jsonify({"error": "property_label required"}), 400

    with open(CUSTOMERS, encoding="utf-8") as f:
        db = json.load(f)

    # Generate key
    base_key = re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')
    key      = base_key
    n        = 2
    while key in db:
        key = f"{base_key}_{n}"
        n  += 1

    db[key] = {
        "property_label": label,
        "bill_to_name":   body.get("bill_to_name", ""),
        "address":        body.get("address", ""),
        "phone":          body.get("phone", ""),
        "email":          body.get("email", ""),
        "hourly_rate":    float(body.get("hourly_rate", 70.0)),
        "customer_type":  body.get("customer_type", "individual"),
        "aliases":        body.get("aliases", []),
    }
    _save_customers(db)
    return jsonify({"ok": True, "key": key})


@app.route("/customers/<key>", methods=["PUT"])
def api_customer_update(key):
    with open(CUSTOMERS, encoding="utf-8") as f:
        db = json.load(f)
    if key not in db:
        return jsonify({"error": "Not found"}), 404

    body = request.get_json()
    cust = db[key]
    for field in ("property_label", "bill_to_name", "address", "phone", "email", "customer_type"):
        if field in body:
            cust[field] = body[field]
    if "hourly_rate" in body:
        cust["hourly_rate"] = float(body["hourly_rate"])
    if "aliases" in body:
        cust["aliases"] = body["aliases"]

    _save_customers(db)
    return jsonify({"ok": True})


@app.route("/customers/<key>", methods=["DELETE"])
def api_customer_delete(key):
    with open(CUSTOMERS, encoding="utf-8") as f:
        db = json.load(f)
    if key not in db:
        return jsonify({"error": "Not found"}), 404
    del db[key]
    _save_customers(db)
    return jsonify({"ok": True})


@app.route("/hours/upload", methods=["POST"])
def api_hours_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400

    f       = request.files["file"]
    content = f.read()

    with open(CUSTOMERS, encoding="utf-8") as cf:
        db = json.load(cf)

    period, entries = parse_hours_csv(content)

    matched   = 0
    unmatched = 0
    for entry in entries:
        key = match_customer_key(entry["job_raw"], db)
        entry["customer_key"] = key
        if key:
            entry["property_label"] = db[key].get("property_label", "")
            matched += 1
        else:
            unmatched += 1

    total_hours = round(sum(e["hours"] for e in entries), 2)
    return jsonify({
        "period":      period,
        "entries":     entries,
        "total_hours": total_hours,
        "matched":     matched,
        "unmatched":   unmatched,
    })


@app.route("/hours/save", methods=["POST"])
def api_hours_save():
    body    = request.get_json()
    entries = body.get("entries", [])
    period  = body.get("period", "")

    # Derive period from first entry date if not provided
    if not period and entries:
        first_date = entries[0].get("date", "")
        m = re.match(r'(\d{4})-(\d{2})', first_date)
        if m:
            period = f"{m.group(1)}_{m.group(2)}"

    # Update property_label from db and auto-learn job names as aliases
    with open(CUSTOMERS, encoding="utf-8") as f:
        db = json.load(f)

    aliases_added = 0
    for entry in entries:
        key = entry.get("customer_key", "")
        if not key or key not in db:
            continue
        entry["property_label"] = db[key].get("property_label", "")
        job_raw = entry.get("job_raw", "").strip()
        if not job_raw:
            continue
        existing_lower = {a.strip().lower() for a in db[key].get("aliases", [])}
        if job_raw.lower() not in existing_lower:
            db[key].setdefault("aliases", []).append(job_raw)
            aliases_added += 1

    if aliases_added:
        _save_customers(db)

    total_hours = round(sum(float(e.get("hours", 0)) for e in entries), 2)

    record = {
        "id":          uuid.uuid4().hex[:10],
        "saved_at":    datetime.datetime.now().isoformat(timespec="seconds"),
        "period":      period,
        "entries":     entries,
        "total_hours": total_hours,
    }

    out_file = HOURS / f"hours_{period}.json"
    tmp      = out_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(out_file))

    return jsonify({"ok": True, "file": out_file.name, "id": record["id"], "aliases_added": aliases_added})


@app.route("/hours")
def api_hours_list():
    files = sorted(HOURS.glob("hours_*.json"), reverse=True)
    out   = []
    for fp in files:
        try:
            rec = json.loads(fp.read_text(encoding="utf-8"))
            out.append({
                "file":        fp.name,
                "period":      rec.get("period", fp.stem.replace("hours_", "")),
                "total_hours": rec.get("total_hours", 0),
                "count":       len(rec.get("entries", [])),
            })
        except Exception:
            pass
    return jsonify(out)


@app.route("/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400

    f   = request.files["file"]
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"Unsupported type: {ext}"}), 400

    uid      = uuid.uuid4().hex[:10]
    filename = secure_filename(uid + ext)
    dest     = UPLOADS / filename
    f.save(str(dest))

    lines, items = ocr_file(dest)
    vendor, date = guess_meta(lines)

    return jsonify({
        "filename": filename,
        "vendor":   vendor,
        "date":     date,
        "lines":    lines[:50],
        "items":    items,
        "ocr_ran":  bool(lines),
    })


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(str(UPLOADS), filename)


@app.route("/save", methods=["POST"])
def api_save():
    body         = request.get_json()
    receipt_date = body.get("receipt_date", "")
    try:
        dt = datetime.datetime.strptime(receipt_date, "%Y-%m-%d")
    except ValueError:
        dt = datetime.datetime.today()

    month_file = EXPENSES / f"expenses_{dt.year}_{dt.month:02d}.json"
    records    = json.loads(month_file.read_text(encoding="utf-8")) if month_file.exists() else []

    with open(CUSTOMERS, encoding="utf-8") as f:
        db = json.load(f)

    items = body.get("items", [])
    for item in items:
        ck = item.get("customer_key", "")
        item["property_label"] = db.get(ck, {}).get("property_label", "")

    record = {
        "id":            uuid.uuid4().hex[:10],
        "saved_at":      datetime.datetime.now().isoformat(timespec="seconds"),
        "receipt_date":  receipt_date,
        "vendor":        body.get("vendor", ""),
        "single_job":    body.get("single_job", False),
        "receipt_file":  body.get("filename", ""),
        "items":         items,
        "receipt_total": round(sum(i.get("amount", 0) for i in items), 2),
    }

    records.append(record)
    tmp = month_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(month_file))

    return jsonify({"ok": True, "file": month_file.name, "id": record["id"]})


@app.route("/expenses/<path:filename>")
def download_expense(filename):
    return send_from_directory(str(EXPENSES), filename, as_attachment=True)


@app.route("/expenses")
def api_expenses():
    files = sorted(EXPENSES.glob("expenses_*.json"), reverse=True)
    out   = []
    for fp in files:
        recs  = json.loads(fp.read_text(encoding="utf-8"))
        parts = fp.stem.replace("expenses_", "").split("_")
        try:
            month_label = datetime.datetime(int(parts[0]), int(parts[1]), 1).strftime("%B %Y")
        except (ValueError, IndexError):
            month_label = fp.stem.replace("expenses_", "").replace("_", "/")
        out.append({
            "file":    fp.name,
            "month":   month_label,
            "period":  fp.stem.replace("expenses_", ""),
            "count":   len(recs),
            "total":   round(sum(sum(i.get("amount", 0) for i in r.get("items", [])) for r in recs), 2),
            "records": recs,
        })
    return jsonify(out)


@app.route("/hours/<period>")
def api_hours_period(period):
    fp = HOURS / f"hours_{period}.json"
    if not fp.exists():
        return jsonify({"error": "not found"}), 404
    return jsonify(json.loads(fp.read_text(encoding="utf-8")))


if __name__ == "__main__":
    print("\n  Small Jobs - Receipt Manager")
    print("  http://localhost:5001\n")
    app.run(debug=True, port=5001, use_reloader=False)
