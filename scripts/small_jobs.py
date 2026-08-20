# -*- coding: utf-8 -*-
"""
Small Jobs - Receipt Assignment Web App
Run:  python scripts/receipt_app.py
Open: http://localhost:5001
"""

import json, os, re, uuid, datetime, calendar, smtplib, ssl, difflib, threading
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_DIR  = Path(__file__).resolve().parent.parent
UPLOADS   = BASE_DIR / "data" / "receipts" / "pending"
EXPENSES  = BASE_DIR / "data" / "expenses"
HOURS     = BASE_DIR / "data" / "hours"
INVOICES  = BASE_DIR / "data" / "invoices"
CUSTOMERS = BASE_DIR / "data" / "customers" / "customers.json"
TASKS     = BASE_DIR / "data" / "tasks" / "tasks.json"
VENDORS   = BASE_DIR / "data" / "vendors.json"
TEMPLATES = Path(__file__).resolve().parent / "templates"

# ── Receipt inbox/reviewed — configurable via .env (defaults to local data/) ──
_inbox_env    = os.environ.get("RECEIPT_INBOX",    "")
_reviewed_env = os.environ.get("RECEIPT_REVIEWED", "")
_canceled_env = os.environ.get("RECEIPT_CANCELED", "")
INBOX    = Path(_inbox_env)    if _inbox_env    else BASE_DIR / "data" / "receipts" / "inbox"
REVIEWED = Path(_reviewed_env) if _reviewed_env else None
CANCELED = Path(_canceled_env) if _canceled_env else None

# ── Email config (set these in your environment or a .env file) ───────────────
SMTP_HOST  = os.environ.get("SMTP_HOST",  "smtp.gmail.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT",  "587"))
SMTP_USER  = os.environ.get("SMTP_USER",  "")   # your Gmail / SMTP address
SMTP_PASS  = os.environ.get("SMTP_PASS",  "")   # app password (not login password)
EMAIL_FROM = os.environ.get("EMAIL_FROM", "") or SMTP_USER

for d in (UPLOADS, INBOX, EXPENSES, HOURS, INVOICES, TASKS.parent):
    d.mkdir(parents=True, exist_ok=True)
if REVIEWED:
    REVIEWED.mkdir(parents=True, exist_ok=True)
if CANCELED:
    CANCELED.mkdir(parents=True, exist_ok=True)

_dropbox_inv_env = os.environ.get("DROPBOX_INVOICES", "")
DROPBOX_INVOICES = Path(_dropbox_inv_env) if _dropbox_inv_env else None
if DROPBOX_INVOICES:
    DROPBOX_INVOICES.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder=str(TEMPLATES))
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB max upload

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".pdf", ".heic", ".webp"}

# ── Tesseract setup ──────────────────────────────────────────────────────────
_TESS_PATHS = [
    # Windows
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"C:\Users\Dandy admin\AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
    # macOS — Apple Silicon (Homebrew)
    "/opt/homebrew/bin/tesseract",
    # macOS — Intel (Homebrew)
    "/usr/local/bin/tesseract",
]

# ── Vendor normalization ──────────────────────────────────────────────────────

def load_vendors():
    if VENDORS.exists():
        try:
            return json.loads(VENDORS.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []

def save_vendors(vendors):
    VENDORS.write_text(json.dumps(sorted(vendors, key=str.lower), indent=2), encoding="utf-8")

def normalize_vendor(raw, vendors):
    """Return canonical vendor name if OCR output closely matches a known vendor."""
    if not raw or not vendors:
        return raw
    raw_lower = raw.lower()
    # Pass 1: substring — handles garbage around the real name
    for v in vendors:
        if v.lower() in raw_lower:
            return v
    # Pass 2: fuzzy — handles OCR typos / dropped characters
    best_v, best_r = None, 0.0
    for v in vendors:
        r = difflib.SequenceMatcher(None, raw_lower, v.lower()).ratio()
        if r > best_r:
            best_r, best_v = r, v
    return best_v if best_r >= 0.72 else raw


def _init_tesseract():
    try:
        import pytesseract
        from PIL import Image  # noqa — just verify Pillow is present
        for p in _TESS_PATHS:
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
               "come again", "receipt", "customer copy", "merchant copy",
               "order summary", "order details"}



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
    # Pre-scan: detect known receipt formats before falling back to first-line heuristic
    all_text = " ".join(lines).lower()
    if "amazon.com" in all_text or ("order summary" in all_text and "order #" in all_text):
        vendor = "Amazon"
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
                for fmt in ("%m/%d/%y", "%m-%d-%y", "%m/%d/%Y", "%m-%d-%Y"):
                    try:
                        date = datetime.datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        pass
                if not date:
                    date = raw
        if not date:
            # Named-month format: "June 23, 2026" or "Order placed June 23, 2026"
            m = re.search(
                r"(January|February|March|April|May|June|July|August|September|October|November|December)"
                r"\s+(\d{1,2}),?\s+(\d{4})", line, re.IGNORECASE)
            if m:
                try:
                    date = datetime.datetime.strptime(
                        f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y"
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    pass
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

def _load_customers():
    with open(CUSTOMERS, encoding="utf-8-sig") as f:
        return json.load(f)

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


# ── Mojibake repair (UTF-8 decoded as Windows-1252) ───────────────────────────
def _fix_mojibake(s):
    if not isinstance(s, str):
        return s
    try:
        fixed = s.encode("windows-1252").decode("utf-8")
        return fixed if fixed != s else s
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s

# ── Hours CSV parser ─────────────────────────────────────────────────────────

def parse_hours_csv(content_bytes):
    """Parse Easy Hours CSV export. Returns (period, entries)."""
    # ── Encoding detection ────────────────────────────────────────────────────
    # EasyHours on Windows often exports UTF-16 LE (with or without BOM).
    # Python's 'utf-16' codec requires a BOM; without it we fall to latin-1
    # which gives garbled text. Detect UTF-16 LE via BOM or null-byte pattern.
    text = None

    if content_bytes[:2] == b'\xff\xfe':           # UTF-16 LE BOM
        try:
            text = content_bytes.decode('utf-16-le')
            text = text.lstrip('﻿')
        except Exception:
            pass
    elif content_bytes[:2] == b'\xfe\xff':         # UTF-16 BE BOM
        try:
            text = content_bytes.decode('utf-16-be')
            text = text.lstrip('﻿')
        except Exception:
            pass

    if text is None and len(content_bytes) >= 10:
        # Heuristic: in UTF-16 LE ASCII, every odd byte is 0x00
        sample    = content_bytes[:min(200, len(content_bytes))]
        pairs     = len(sample) // 2
        odd_nulls = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
        if pairs and odd_nulls / pairs > 0.6:      # >60% → almost certainly UTF-16 LE
            try:
                text = content_bytes.decode('utf-16-le')
            except Exception:
                pass

    if text is None:
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                candidate = content_bytes.decode(enc)
                if '\x00' not in candidate:         # Reject garbled UTF-16-as-latin-1
                    text = candidate
                    break
            except Exception:
                continue

    if text is None:
        return "", []

    # ── CSV parsing ───────────────────────────────────────────────────────────
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
        # Skip fully blank rows
        if not row or not any(cell.strip() for cell in row):
            continue

        first = row[0].strip().strip('"') if row else ''

        # Skip header row
        if first.lower() == "date":
            continue

        # Skip #ERROR! rows and EasyHours footer lines
        if first.startswith('#') or 'easyhours' in first.lower() or 'copyright' in first.lower():
            continue

        # Skip daily total rows (col 1 == "Total")
        job_val = row[1].strip().strip('"') if len(row) > 1 else ''
        if job_val.lower() == "total":
            continue

        # Try to parse col 0 as a date
        date_str = ""
        try:
            dt = datetime.datetime.strptime(first, "%m/%d/%y")
            date_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            # Not a date — treat as memo continuation if it has content
            # (but skip period-header lines like "June 1, 2026 – June 30, 2026")
            if entries and first and not re.search(r'([A-Za-z]+)\s+\d+,\s+\d{4}', first):
                prev_memo = entries[-1].get("memo", "")
                extra = " ".join(cell.strip() for cell in row[1:] if cell.strip())
                line = (first + (" " + extra if extra else "")).strip()
                entries[-1]["memo"] = (prev_memo + "\n" + line).strip() if prev_memo else line
            continue

        # Valid date row — extract fields
        in_time   = row[2].strip().strip('"') if len(row) > 2 else ""
        out_time  = row[3].strip().strip('"') if len(row) > 3 else ""
        hours_raw = row[5].strip().strip('"') if len(row) > 5 else ""
        memo      = row[7].strip().strip('"') if len(row) > 7 else ""

        # Parse hours — strip trailing 'h'
        hours = 0.0
        try:
            hours = float(hours_raw.rstrip("h").strip())
        except ValueError:
            pass

        if not job_val:
            continue

        entries.append({
            "date":           date_str,
            "job_raw":        _fix_mojibake(job_val),
            "customer_key":   "",
            "property_label": "",
            "in_time":        in_time,
            "out_time":       out_time,
            "hours":          hours,
            "memo":           _fix_mojibake(memo),
        })

    return period, entries


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("small_jobs.html", email_from=EMAIL_FROM)

@app.route("/sw.js")
def service_worker():
    return send_from_directory(str(Path(__file__).parent / "static"), "sw.js",
                               mimetype="application/javascript")

@app.route("/manifest.json")
def manifest():
    return send_from_directory(str(Path(__file__).parent / "static"), "manifest.json",
                               mimetype="application/manifest+json")


@app.route("/customers")
def api_customers():
    with open(CUSTOMERS, encoding="utf-8-sig") as f:
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
    with open(CUSTOMERS, encoding="utf-8-sig") as f:
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

    # Assign a stable ID to each entry so it can be edited later
    for entry in entries:
        if not entry.get("entry_id"):
            entry["entry_id"] = uuid.uuid4().hex[:10]

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
            rec = json.loads(fp.read_text(encoding="utf-8-sig"))
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
    vendor = normalize_vendor(vendor, load_vendors())

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


# ── Receipts inbox (auto-import queue) ───────────────────────────────────────

@app.route("/receipts/inbox")
def get_receipts_inbox():
    items = []
    for f in sorted(INBOX.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix.lower() in ALLOWED_EXT:
            stat = f.stat()
            items.append({
                "filename": f.name,
                "size":     stat.st_size,
                "added":    stat.st_ctime,
            })
    return jsonify(items)


@app.route("/receipts/inbox/<path:filename>/review", methods=["POST"])
def review_inbox_item(filename):
    import shutil
    src = INBOX / filename
    if not src.exists():
        return jsonify({"error": "not found"}), 404
    dest_name = str(uuid.uuid4()) + src.suffix.lower()
    dest = UPLOADS / dest_name
    shutil.copy2(str(src), str(dest))
    lines, items   = ocr_file(dest)
    vendor, date   = guess_meta(lines)
    vendor = normalize_vendor(vendor, load_vendors())
    return jsonify({
        "filename":     dest_name,
        "vendor":       vendor,
        "date":         date,
        "lines":        lines[:50],
        "items":        items,
        "ocr_ran":      bool(lines),
        "inbox_source": filename,
    })


@app.route("/receipts/inbox/<path:filename>", methods=["DELETE"])
def delete_inbox_item(filename):
    f = INBOX / filename
    if f.exists():
        f.unlink()
    return jsonify({"ok": True})


@app.route("/receipts/inbox/<path:filename>/reviewed", methods=["POST"])
def mark_inbox_reviewed(filename):
    import shutil
    src = INBOX / filename
    if not src.exists():
        return jsonify({"ok": True})
    if REVIEWED:
        dest = REVIEWED / filename
        if dest.exists():
            stem = dest.stem
            dest = REVIEWED / f"{stem}_{uuid.uuid4().hex[:6]}{dest.suffix}"
        shutil.move(str(src), str(dest))
    else:
        src.unlink()
    return jsonify({"ok": True})


@app.route("/receipts/inbox/<path:filename>/canceled", methods=["POST"])
def mark_inbox_canceled(filename):
    import shutil
    src = INBOX / filename
    if not src.exists():
        return jsonify({"ok": True})
    if CANCELED:
        dest = CANCELED / filename
        if dest.exists():
            stem = dest.stem
            dest = CANCELED / f"{stem}_{uuid.uuid4().hex[:6]}{dest.suffix}"
        shutil.move(str(src), str(dest))
    else:
        src.unlink()
    return jsonify({"ok": True})


# ── Vendor list CRUD ─────────────────────────────────────────────────────────

@app.route("/vendors", methods=["GET"])
def api_get_vendors():
    return jsonify(load_vendors())

@app.route("/vendors", methods=["POST"])
def api_add_vendor():
    name = (request.get_json() or {}).get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name required"})
    vendors = load_vendors()
    if name not in vendors:
        vendors.append(name)
        save_vendors(vendors)
    return jsonify({"ok": True, "vendors": load_vendors()})

@app.route("/vendors/<path:name>", methods=["PUT"])
def api_update_vendor(name):
    new_name = (request.get_json() or {}).get("name", "").strip()
    if not new_name:
        return jsonify({"ok": False, "error": "Name required"})
    vendors = load_vendors()
    vendors = [new_name if v == name else v for v in vendors]
    save_vendors(vendors)
    return jsonify({"ok": True, "vendors": load_vendors()})

@app.route("/vendors/<path:name>", methods=["DELETE"])
def api_delete_vendor(name):
    vendors = [v for v in load_vendors() if v != name]
    save_vendors(vendors)
    return jsonify({"ok": True, "vendors": vendors})


@app.route("/save", methods=["POST"])
def api_save():
    body         = request.get_json()
    receipt_date = body.get("receipt_date", "")
    try:
        dt = datetime.datetime.strptime(receipt_date, "%Y-%m-%d")
    except ValueError:
        dt = datetime.datetime.today()

    month_file = EXPENSES / f"expenses_{dt.year}_{dt.month:02d}.json"
    records    = json.loads(month_file.read_text(encoding="utf-8-sig")) if month_file.exists() else []

    with open(CUSTOMERS, encoding="utf-8") as f:
        db = json.load(f)

    items = body.get("items", [])
    for item in items:
        ck = item.get("customer_key", "")
        item["property_label"] = db.get(ck, {}).get("property_label", "")

    record = {
        "id":             uuid.uuid4().hex[:10],
        "saved_at":       datetime.datetime.now().isoformat(timespec="seconds"),
        "receipt_date":   receipt_date,
        "vendor":         body.get("vendor", ""),
        "single_job":     body.get("single_job", False),
        "receipt_file":   body.get("filename", ""),
        "items":          items,
        "receipt_total":  body.get("receipt_total") or round(sum(i.get("amount", 0) for i in items), 2),
        "pending_review": bool(body.get("pending_review", False)),
    }

    records.append(record)
    tmp = month_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(month_file))

    cust_keys = {i.get("customer_key","") for i in items if i.get("customer_key","")}
    _mark_invoices_stale(f"{dt.year}_{dt.month:02d}", cust_keys)
    return jsonify({"ok": True, "file": month_file.name, "id": record["id"]})


@app.route("/expenses/<path:filename>")
def download_expense(filename):
    return send_from_directory(str(EXPENSES), filename, as_attachment=True)


@app.route("/expenses/vendor-summary")
def expenses_vendor_summary():
    db      = _load_customers()
    periods = []
    vendors = {}
    for ep in sorted(EXPENSES.glob("expenses_*.json")):
        period = ep.stem.replace("expenses_", "")
        try:
            receipts = json.loads(ep.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        has_data = False
        for r in receipts:
            vendor = (r.get("vendor") or "Unknown").strip() or "Unknown"
            for item in r.get("items", []):
                amt = float(item.get("amount") or 0)
                if not amt:
                    continue
                ck    = item.get("customer_key", "")
                ctype = db.get(ck, {}).get("customer_type", "individual")
                cat   = "personal" if ctype == "personal" else "tools" if ctype == "tools" else "customer"
                vendors.setdefault(vendor, {}).setdefault(period, {"customer": 0.0, "personal": 0.0, "tools": 0.0})
                vendors[vendor][period][cat] = round(vendors[vendor][period][cat] + amt, 2)
                has_data = True
        if has_data and period not in periods:
            periods.append(period)
    return jsonify({"periods": sorted(periods), "vendors": vendors})


@app.route("/expenses")
def api_expenses():
    files = sorted(EXPENSES.glob("expenses_*.json"), reverse=True)
    out   = []
    for fp in files:
        recs  = json.loads(fp.read_text(encoding="utf-8-sig"))
        parts = fp.stem.replace("expenses_", "").split("_")
        try:
            month_label = datetime.datetime(int(parts[0]), int(parts[1]), 1).strftime("%B %Y")
        except (ValueError, IndexError):
            month_label = fp.stem.replace("expenses_", "").replace("_", "/")
        unique_custs = len({i.get("customer_key", "") for r in recs for i in r.get("items", []) if i.get("customer_key", "")})
        out.append({
            "file":      fp.name,
            "month":     month_label,
            "period":    fp.stem.replace("expenses_", ""),
            "count":     len(recs),
            "customers": unique_custs,
            "total":     round(sum(sum(i.get("amount", 0) for i in r.get("items", [])) for r in recs), 2),
            "records":   recs,
        })
    return jsonify(out)


@app.route("/receipts/<period>/<receipt_id>", methods=["GET"])
def get_receipt(period, receipt_id):
    fp = EXPENSES / f"expenses_{period}.json"
    if not fp.exists():
        return jsonify({"error": "not found"}), 404
    records = json.loads(fp.read_text(encoding="utf-8-sig"))
    record  = next((r for r in records if r.get("id") == receipt_id), None)
    if not record:
        return jsonify({"error": "not found"}), 404
    return jsonify(record)


@app.route("/receipts/<period>/<receipt_id>", methods=["PUT"])
def update_receipt(period, receipt_id):
    fp = EXPENSES / f"expenses_{period}.json"
    if not fp.exists():
        return jsonify({"error": "not found"}), 404
    records = json.loads(fp.read_text(encoding="utf-8-sig"))
    idx     = next((i for i, r in enumerate(records) if r.get("id") == receipt_id), None)
    if idx is None:
        return jsonify({"error": "not found"}), 404

    body = request.get_json()
    with open(CUSTOMERS, encoding="utf-8") as f:
        db = json.load(f)

    items = body.get("items", [])
    for item in items:
        ck = item.get("customer_key", "")
        item["property_label"] = db.get(ck, {}).get("property_label", "")

    records[idx].update({
        "receipt_date":   body.get("receipt_date",   records[idx].get("receipt_date", "")),
        "vendor":         body.get("vendor",          records[idx].get("vendor", "")),
        "single_job":     body.get("single_job",      records[idx].get("single_job", False)),
        "items":          items,
        "receipt_total":  body.get("receipt_total") or round(sum(i.get("amount", 0) for i in items), 2),
        "pending_review": bool(body.get("pending_review", False)),
    })

    # Check if the date moved to a different month — if so, migrate the record
    new_date = records[idx].get("receipt_date", "")
    try:
        dt = datetime.datetime.strptime(new_date, "%Y-%m-%d")
        new_period = f"{dt.year}_{dt.month:02d}"
    except ValueError:
        new_period = period

    if new_period != period:
        # Remove from old file
        updated_record = records.pop(idx)
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(fp))
        # Append to new period file
        new_fp = EXPENSES / f"expenses_{new_period}.json"
        new_records = json.loads(new_fp.read_text(encoding="utf-8-sig")) if new_fp.exists() else []
        new_records.append(updated_record)
        tmp2 = new_fp.with_suffix(".tmp")
        tmp2.write_text(json.dumps(new_records, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp2), str(new_fp))
    else:
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(fp))

    cust_keys = {i.get("customer_key","") for i in items if i.get("customer_key","")}
    _mark_invoices_stale(new_period, cust_keys)
    return jsonify({"ok": True, "period": new_period})


@app.route("/receipts/<period>/<receipt_id>", methods=["DELETE"])
def delete_receipt(period, receipt_id):
    fp = EXPENSES / f"expenses_{period}.json"
    if not fp.exists():
        return jsonify({"error": "not found"}), 404
    records = json.loads(fp.read_text(encoding="utf-8-sig"))
    before  = len(records)
    records = [r for r in records if r.get("id") != receipt_id]
    if len(records) == before:
        return jsonify({"error": "not found"}), 404
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(fp))
    return jsonify({"ok": True})


@app.route("/hours/<period>")
def api_hours_period(period):
    fp = HOURS / f"hours_{period}.json"
    if not fp.exists():
        return jsonify({"error": "not found"}), 404
    record  = json.loads(fp.read_text(encoding="utf-8-sig"))
    entries = record.get("entries", [])
    # Back-fill entry_ids for files saved before this feature existed
    if any("entry_id" not in e for e in entries):
        for e in entries:
            if "entry_id" not in e:
                e["entry_id"] = uuid.uuid4().hex[:10]
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(fp))
    return jsonify(record)


@app.route("/hours/<period>/entries/<entry_id>", methods=["PUT"])
def update_hours_entry(period, entry_id):
    fp = HOURS / f"hours_{period}.json"
    if not fp.exists():
        return jsonify({"error": "not found"}), 404
    record  = json.loads(fp.read_text(encoding="utf-8-sig"))
    entries = record.get("entries", [])
    idx     = next((i for i, e in enumerate(entries) if e.get("entry_id") == entry_id), None)
    if idx is None:
        return jsonify({"error": "not found"}), 404
    body = request.get_json() or {}
    for field in ("date", "in_time", "out_time", "hours", "memo", "customer_key", "property_label", "job_raw"):
        if field in body:
            entries[idx][field] = body[field]
    entry_ck = entries[idx].get("customer_key", "")  # capture before possible pop

    # Check if the date crossed into a different month — migrate if so
    new_date = entries[idx].get("date", "")
    try:
        dt         = datetime.datetime.strptime(new_date, "%Y-%m-%d")
        new_period = f"{dt.year}_{dt.month:02d}"
    except ValueError:
        new_period = period

    if new_period != period:
        migrated = entries.pop(idx)
        record["total_hours"] = round(sum(float(e.get("hours", 0)) for e in entries), 2)
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(fp))

        new_fp = HOURS / f"hours_{new_period}.json"
        if new_fp.exists():
            new_record = json.loads(new_fp.read_text(encoding="utf-8-sig"))
        else:
            new_record = {"period": new_period, "total_hours": 0, "entries": []}
        new_record["entries"].append(migrated)
        new_record["total_hours"] = round(sum(float(e.get("hours", 0)) for e in new_record["entries"]), 2)
        tmp2 = new_fp.with_suffix(".tmp")
        tmp2.write_text(json.dumps(new_record, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp2), str(new_fp))
    else:
        record["total_hours"] = round(sum(float(e.get("hours", 0)) for e in entries), 2)
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(fp))

    if entry_ck:
        _mark_invoices_stale(new_period, {entry_ck})
    return jsonify({"ok": True, "period": new_period})


@app.route("/hours/<period>/bulk-assign", methods=["POST"])
def bulk_assign_hours(period):
    body         = request.get_json() or {}
    entry_ids    = set(body.get("entry_ids", []))
    customer_key = body.get("customer_key", "")
    if not entry_ids or not customer_key:
        return jsonify({"error": "entry_ids and customer_key required"}), 400
    fp = HOURS / f"hours_{period}.json"
    if not fp.exists():
        return jsonify({"error": "not found"}), 404
    record  = json.loads(fp.read_text(encoding="utf-8-sig"))
    updated = 0
    for e in record.get("entries", []):
        if e.get("entry_id") in entry_ids:
            e["customer_key"] = customer_key
            updated += 1
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(fp))
    return jsonify({"ok": True, "updated": updated})


@app.route("/hours/<period>/entries/<entry_id>", methods=["DELETE"])
def delete_hours_entry(period, entry_id):
    fp = HOURS / f"hours_{period}.json"
    if not fp.exists():
        return jsonify({"error": "not found"}), 404
    record     = json.loads(fp.read_text(encoding="utf-8-sig"))
    entries    = record.get("entries", [])
    new_entries = [e for e in entries if e.get("entry_id") != entry_id]
    if len(new_entries) == len(entries):
        return jsonify({"error": "not found"}), 404
    record["entries"]     = new_entries
    record["total_hours"] = round(sum(float(e.get("hours", 0)) for e in new_entries), 2)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(fp))
    return jsonify({"ok": True})


@app.route("/hours/<period>/entries", methods=["POST"])
def create_hours_entry(period):
    body = request.get_json() or {}
    # Derive the correct period from the entry's date so a July date on
    # the June tab still lands in the right file.
    date_str       = body.get("date", "")
    actual_period  = period
    if date_str:
        try:
            dt            = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            actual_period = f"{dt.year}_{dt.month:02d}"
        except ValueError:
            pass
    fp = HOURS / f"hours_{actual_period}.json"
    if fp.exists():
        record = json.loads(fp.read_text(encoding="utf-8-sig"))
    else:
        record = {"period": actual_period, "entries": [], "total_hours": 0.0}
    entry = {
        "entry_id":       uuid.uuid4().hex[:10],
        "date":           date_str,
        "job_raw":        body.get("job_raw", ""),
        "customer_key":   body.get("customer_key", ""),
        "property_label": body.get("property_label", ""),
        "in_time":        body.get("in_time", ""),
        "out_time":       body.get("out_time", ""),
        "hours":          float(body.get("hours") or 0),
        "memo":           body.get("memo", ""),
    }
    record["entries"].append(entry)
    record["total_hours"] = round(sum(float(e.get("hours", 0)) for e in record["entries"]), 2)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(fp))
    ck = body.get("customer_key", "")
    if ck:
        _mark_invoices_stale(actual_period, {ck})
    return jsonify({"ok": True, "entry_id": entry["entry_id"], "period": actual_period})


@app.route("/hours/resync-labels", methods=["POST"])
def resync_labels():
    """Re-derive property_label on all hours entries from the live customer record."""
    db = _load_customers()
    updated_files = 0
    updated_entries = 0
    for fp in sorted(HOURS.glob("hours_*.json")):
        try:
            rec = json.loads(fp.read_text(encoding="utf-8-sig"))
            changed = 0
            for entry in rec.get("entries", []):
                ck = entry.get("customer_key", "")
                if ck and ck in db:
                    correct = db[ck].get("property_label", "")
                    if entry.get("property_label", "") != correct:
                        entry["property_label"] = correct
                        changed += 1
            if changed:
                tmp = fp.with_suffix(".tmp")
                tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
                os.replace(str(tmp), str(fp))
                updated_files += 1
                updated_entries += changed
        except Exception:
            pass
    return jsonify({"ok": True, "files": updated_files, "entries": updated_entries})


# ── Invoice helpers ──────────────────────────────────────────────────────────

def _invoice_meta():
    mp = INVOICES / "meta.json"
    if mp.exists():
        return json.loads(mp.read_text(encoding="utf-8-sig"))
    return {"last_invoice_number": 119, "last_year": None}

def _save_invoice_meta(meta):
    tmp = (INVOICES / "meta.json").with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(INVOICES / "meta.json"))

def _next_invoice_id(year, month):
    meta = _invoice_meta()
    key = f"{year}_{month:02d}"
    counters = meta.setdefault("monthly_counters", {})
    if key not in counters:
        counters[key] = 120   # first invoice in the month increments to 121
    counters[key] += 1
    _save_invoice_meta(meta)
    return f"{year}{month:02d}{counters[key]:03d}"

def _load_invoices(period):
    fp = INVOICES / f"invoices_{period}.json"
    if not fp.exists():
        return []
    return json.loads(fp.read_text(encoding="utf-8-sig"))

def _save_invoices(period, invoices):
    fp = INVOICES / f"invoices_{period}.json"
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(invoices, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(fp))


_BROWSER_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",  # prefer Edge — unaffected by running Chrome
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
_BROWSER_PROFILE = Path(os.environ.get("TEMP", "C:/Temp")) / "small-jobs-headless-profile"

def _find_browser():
    for p in _BROWSER_PATHS:
        if Path(p).exists():
            return p
    return None

def _try_save_invoice_pdf(invoice_id, inv, period):
    """Render invoice to PDF using headless Edge/Chrome — identical to browser print view."""
    if not DROPBOX_INVOICES:
        app.logger.warning("PDF save skipped: DROPBOX_INVOICES not configured (restart server after .env change)")
        return
    if not DROPBOX_INVOICES.exists():
        app.logger.warning(f"PDF save skipped: folder does not exist: {DROPBOX_INVOICES}")
        return
    browser = _find_browser()
    if not browser:
        app.logger.warning("PDF save skipped: no Chrome or Edge found")
        return
    try:
        import tempfile, subprocess
        inv_data = dict(inv)
        if not inv_data.get("month_label"):
            try:
                y, m = int(period.split("_")[0]), int(period.split("_")[1])
                inv_data["month_label"] = datetime.datetime(y, m, 1).strftime("%B %Y")
            except Exception:
                inv_data["month_label"] = period
        if not inv_data.get("property_label"):
            try:
                db = _load_customers()
                cust = db.get(inv_data.get("customer_key", ""), {})
                inv_data["property_label"] = cust.get("property_label", "")
            except Exception:
                pass
        html = render_template("invoice_print.html", inv=inv_data)
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False,
                                         mode="w", encoding="utf-8") as f:
            f.write(html)
            tmp_path = f.name
        dest = str(DROPBOX_INVOICES / f"{invoice_id}.pdf")
        file_url = Path(tmp_path).as_uri()
        result = subprocess.run([
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            "--run-all-compositor-stages-before-draw",
            f"--user-data-dir={_BROWSER_PROFILE}",
            f"--print-to-pdf={dest}",
            file_url,
        ], capture_output=True, timeout=30)
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        if result.returncode == 0 and Path(dest).exists():
            app.logger.info(f"PDF saved: {dest}")
        else:
            app.logger.error(f"Browser PDF export failed (rc={result.returncode}): {result.stderr.decode(errors='replace')}")
    except Exception as e:
        app.logger.error(f"PDF save failed for {invoice_id}: {e}", exc_info=True)


def _save_invoice_pdf_async(invoice_id, inv, period):
    """Fire PDF generation in a background thread so the HTTP response returns immediately."""
    ctx = app.app_context()
    def _run():
        with ctx:
            _try_save_invoice_pdf(invoice_id, inv, period)
    threading.Thread(target=_run, daemon=True).start()


def _mark_invoices_stale(period, customer_keys):
    """Set stale=True on any non-superseded invoices for customer_keys in period."""
    if not customer_keys:
        return
    inv_file = INVOICES / f"invoices_{period}.json"
    if not inv_file.exists():
        return
    invoices = json.loads(inv_file.read_text(encoding="utf-8-sig"))
    changed = False
    for inv in invoices:
        if not inv.get("superseded") and inv.get("customer_key") in customer_keys:
            if not inv.get("stale"):
                inv["stale"] = True
                changed = True
    if changed:
        _save_invoices(period, invoices)


# ── Invoice helpers ──────────────────────────────────────────────────────────

_NON_BILLABLE_TYPES = {"personal", "tools"}
_NON_BILLABLE_KEYS  = {"personal_tools"}


def _compute_live_invoice_data(period, customer_key, db):
    """Recompute what an invoice would contain right now from live hours + expense data."""
    rate = float(db.get(customer_key, {}).get("hourly_rate", 70))

    hp = HOURS / f"hours_{period}.json"
    raw_hours = json.loads(hp.read_text(encoding="utf-8-sig")).get("entries", []) if hp.exists() else []
    labor_entries = sorted([
        {
            "date":        e.get("date", ""),
            "description": e.get("memo", ""),
            "hours":       float(e.get("hours", 0)),
            "rate":        rate,
            "amount":      round(float(e.get("hours", 0)) * rate, 2),
        }
        for e in raw_hours if e.get("customer_key") == customer_key
    ], key=lambda x: x["date"])

    ep = EXPENSES / f"expenses_{period}.json"
    material_entries = []
    if ep.exists():
        for receipt in json.loads(ep.read_text(encoding="utf-8-sig")):
            assigned = [i.get("customer_key","") for i in receipt.get("items",[]) if i.get("customer_key","")]
            is_split = len(set(assigned)) > 1
            for item in receipt.get("items", []):
                if item.get("customer_key") != customer_key:
                    continue
                desc = item.get("description", "")
                if item.get("is_tax"):
                    pass
                elif is_split:
                    low = desc.lower().strip()
                    desc = "Sales Tax – split receipt" if low in ("sales tax", "tax") else f"{desc} (split receipt)"
                material_entries.append({
                    "date":        receipt.get("receipt_date", ""),
                    "vendor":      receipt.get("vendor", ""),
                    "description": desc,
                    "amount":      float(item.get("amount", 0)),
                })
        material_entries.sort(key=lambda x: x["date"])

    labor_subtotal     = round(sum(e["amount"] for e in labor_entries), 2)
    materials_subtotal = round(sum(m["amount"] for m in material_entries), 2)
    return {
        "labor_entries":     labor_entries,
        "material_entries":  material_entries,
        "labor_subtotal":    labor_subtotal,
        "materials_subtotal": materials_subtotal,
        "total":             round(labor_subtotal + materials_subtotal, 2),
    }


# ── Invoice routes ────────────────────────────────────────────────────────────

@app.route("/invoices")
def api_invoices_list():
    meta             = _invoice_meta()
    net_days_default = meta.get("net_days", 20)
    today            = datetime.date.today()

    def _is_overdue(inv):
        if inv.get("status") != "sent":
            return False
        due = inv.get("due_date")
        if due:
            return datetime.date.fromisoformat(due) < today
        sent = inv.get("sent_at")
        if not sent:
            return False
        net      = inv.get("net_days", net_days_default)
        sent_dt  = datetime.datetime.fromisoformat(sent).date()
        return (today - sent_dt).days > net

    db = _load_customers()
    non_billable_keys = {ck for ck, c in db.items()
                         if c.get("customer_type") in _NON_BILLABLE_TYPES or ck in _NON_BILLABLE_KEYS}

    files = sorted(INVOICES.glob("invoices_*.json"), reverse=True)
    out = []
    for fp in files:
        try:
            period = fp.stem.replace("invoices_", "")
            invs   = json.loads(fp.read_text(encoding="utf-8-sig"))
            active = [i for i in invs if not i.get("superseded")]

            labor_total = round(sum(i.get("labor_subtotal", 0) for i in active), 2)

            tools_total     = 0.0
            personal_total  = 0.0
            materials_total = 0.0  # expenses assigned to real (billable) customers
            ep = EXPENSES / f"expenses_{period}.json"
            if ep.exists():
                for r in json.loads(ep.read_text(encoding="utf-8-sig")):
                    for item in r.get("items", []):
                        ck    = item.get("customer_key", "")
                        amt   = float(item.get("amount", 0))
                        ctype = db.get(ck, {}).get("customer_type", "")
                        if ck in _NON_BILLABLE_KEYS or ctype == "tools":
                            tools_total += amt
                        elif ctype == "personal":
                            personal_total += amt
                        elif ck and ctype not in _NON_BILLABLE_TYPES:
                            materials_total += amt
            tools_total     = round(tools_total, 2)
            personal_total  = round(personal_total, 2)
            materials_total = round(materials_total, 2)

            out.append({
                "period":           period,
                "count":            len(active),
                "draft":            sum(1 for i in active if i.get("status") == "draft"),
                "sent":             sum(1 for i in active if i.get("status") == "sent"),
                "paid":             sum(1 for i in active if i.get("status") == "paid"),
                "overdue":          sum(1 for i in active if _is_overdue(i)),
                "stale":            sum(1 for i in active if i.get("stale")),
                "labor_total":      labor_total,
                "tools_total":      tools_total,
                "personal_total":   personal_total,
                "materials_total":  materials_total,
            })
        except Exception:
            pass
    return jsonify(out)


@app.route("/invoices/settings", methods=["GET"])
def get_invoice_settings():
    meta = _invoice_meta()
    return jsonify({"net_days": meta.get("net_days", 20)})


@app.route("/invoices/settings", methods=["PUT"])
def put_invoice_settings():
    body     = request.get_json() or {}
    meta     = _invoice_meta()
    meta["net_days"] = max(1, int(body.get("net_days", 20)))
    _save_invoice_meta(meta)
    return jsonify({"ok": True, "net_days": meta["net_days"]})


@app.route("/invoices/<period>")
def api_invoices_for_period(period):
    invs   = _load_invoices(period)
    active = [i for i in invs if not i.get("superseded")]
    return jsonify(active)


@app.route("/invoices/<period>/history")
def api_invoices_history(period):
    """All invoices for a period including superseded — used for version history."""
    return jsonify(_load_invoices(period))


@app.route("/invoices/<period>/generate", methods=["POST"])
def generate_invoices(period):
    body            = request.get_json() or {}
    target_customer = body.get("customer_key")  # None = all customers
    db              = _load_customers()

    # Parse period
    year, month  = int(period.split("_")[0]), int(period.split("_")[1])
    last_day     = calendar.monthrange(year, month)[1]
    invoice_date = f"{year}-{month:02d}-{last_day:02d}"
    month_name   = datetime.datetime(year, month, 1).strftime("%B %Y")

    # Net days / due date
    meta     = _invoice_meta()
    net_days = int(body.get("net_days") or meta.get("net_days", 20))
    if net_days != meta.get("net_days"):
        meta["net_days"] = net_days
        _save_invoice_meta(meta)
    due_date = (datetime.date(year, month, last_day) +
                datetime.timedelta(days=net_days)).isoformat()

    # Load hours for period
    hours_entries = []
    hp = HOURS / f"hours_{period}.json"
    if hp.exists():
        hours_entries = json.loads(hp.read_text(encoding="utf-8-sig")).get("entries", [])

    # Load expense line items for period (flattened with receipt-level date/vendor)
    expense_items = []
    ep = EXPENSES / f"expenses_{period}.json"
    if ep.exists():
        for receipt in json.loads(ep.read_text(encoding="utf-8-sig")):
            assigned_keys = [i.get("customer_key","") for i in receipt.get("items",[]) if i.get("customer_key","")]
            is_split      = len(set(assigned_keys)) > 1
            for item in receipt.get("items", []):
                ck = item.get("customer_key", "")
                if not ck:
                    continue
                expense_items.append({
                    "customer_key": ck,
                    "date":         receipt.get("receipt_date", ""),
                    "vendor":       receipt.get("vendor", ""),
                    "description":  item.get("description", ""),
                    "amount":       float(item.get("amount", 0)),
                    "is_split":     is_split,
                })

    # Group labor and materials by customer
    buckets = {}
    for e in hours_entries:
        ck = e.get("customer_key", "")
        if not ck or ck not in db:
            continue
        if target_customer and ck != target_customer:
            continue
        if ck in _NON_BILLABLE_KEYS or db[ck].get("customer_type") in _NON_BILLABLE_TYPES:
            continue
        buckets.setdefault(ck, {"labor": [], "materials": []})["labor"].append(e)

    for item in expense_items:
        ck = item["customer_key"]
        if not ck or ck not in db:
            continue
        if target_customer and ck != target_customer:
            continue
        if ck in _NON_BILLABLE_KEYS or db[ck].get("customer_type") in _NON_BILLABLE_TYPES:
            continue
        buckets.setdefault(ck, {"labor": [], "materials": []})["materials"].append(item)

    existing     = _load_invoices(period)
    active_by_ck = {i["customer_key"]: i for i in existing if not i.get("superseded")}
    now          = datetime.datetime.now().isoformat(timespec="seconds")
    generated    = []

    for ck, bucket in buckets.items():
        cust = db[ck]
        rate = float(cust.get("hourly_rate", 70))

        labor_entries = sorted([
            {
                "date":        e.get("date", ""),
                "description": e.get("memo", ""),
                "hours":       float(e.get("hours", 0)),
                "rate":        rate,
                "amount":      round(float(e.get("hours", 0)) * rate, 2),
            }
            for e in bucket["labor"]
        ], key=lambda x: x["date"])

        def _mat_desc(m):
            if m.get("is_tax"):
                return m["description"]   # already formatted in the receipt UI
            desc = m["description"]
            if m.get("is_split"):
                low = desc.lower().strip()
                if low in ("sales tax", "tax"):
                    desc = "Sales Tax – split receipt"
                else:
                    desc = f"{desc} (split receipt)"
            return desc

        material_entries = sorted([
            {
                "date":        m["date"],
                "vendor":      m["vendor"],
                "description": _mat_desc(m),
                "amount":      m["amount"],
            }
            for m in bucket["materials"]
        ], key=lambda x: x["date"])

        labor_subtotal     = round(sum(e["amount"] for e in labor_entries), 2)
        materials_subtotal = round(sum(m["amount"] for m in material_entries), 2)
        invoice_subtotal   = round(labor_subtotal + materials_subtotal, 2)

        if ck in active_by_ck:
            old = active_by_ck[ck]
            if old.get("status") == "draft":
                # Overwrite draft in-place — refresh contact info too in case customer was edited
                old.update({
                    "bill_to_name":        cust.get("bill_to_name") or cust.get("property_label", ck),
                    "bill_to_address":     cust.get("address", ""),
                    "bill_to_phone":       cust.get("phone", ""),
                    "bill_to_email":       cust.get("email", ""),
                    "labor_entries":       labor_entries,
                    "material_entries":    material_entries,
                    "labor_subtotal":      labor_subtotal,
                    "materials_subtotal":  materials_subtotal,
                    "invoice_subtotal":    invoice_subtotal,
                    "total":               invoice_subtotal,
                    "due_date":            due_date,
                    "net_days":            net_days,
                    "regenerated_at":      now,
                    "version":             old.get("version", 1) + 1,
                    "stale":               False,
                    "generated_at":        now,
                })
                generated.append(old["invoice_id"])
            else:
                # Sent/paid — supersede and create corrected draft
                old["superseded"] = True
                corrected_id = _next_invoice_id(year, month)
                new_inv = _build_invoice(corrected_id, ck, cust, period, invoice_date,
                                         month_name, labor_entries, material_entries,
                                         labor_subtotal, materials_subtotal, invoice_subtotal,
                                         now, corrects=old["invoice_id"],
                                         net_days=net_days, due_date=due_date)
                existing.append(new_inv)
                generated.append(corrected_id)
        else:
            inv_id  = _next_invoice_id(year, month)
            new_inv = _build_invoice(inv_id, ck, cust, period, invoice_date, month_name,
                                     labor_entries, material_entries, labor_subtotal,
                                     materials_subtotal, invoice_subtotal, now,
                                     net_days=net_days, due_date=due_date)
            existing.append(new_inv)
            generated.append(inv_id)

    _save_invoices(period, existing)
    return jsonify({"ok": True, "generated": generated, "period": period})


def _build_invoice(inv_id, ck, cust, period, invoice_date, month_name,
                   labor_entries, material_entries, labor_subtotal,
                   materials_subtotal, invoice_subtotal, now,
                   corrects=None, net_days=20, due_date=None):
    return {
        "invoice_id":          inv_id,
        "customer_key":        ck,
        "customer_type":       cust.get("customer_type", "individual"),
        "property_label":      cust.get("property_label", ""),
        "bill_to_name":        cust.get("bill_to_name") or cust.get("property_label", ck),
        "bill_to_address":     cust.get("address", ""),
        "bill_to_phone":       cust.get("phone", ""),
        "bill_to_email":       cust.get("email", ""),
        "period":              period,
        "month_label":         month_name,
        "invoice_date":        invoice_date,
        "due_date":            due_date,
        "net_days":            net_days,
        "job_description":     f"handy man jobs – {month_name}",
        "status":              "draft",
        "created_at":          now,
        "sent_at":             None,
        "paid_at":             None,
        "version":             1,
        "corrects":            corrects,
        "superseded":          False,
        "labor_entries":       labor_entries,
        "material_entries":    material_entries,
        "labor_subtotal":      labor_subtotal,
        "materials_subtotal":  materials_subtotal,
        "invoice_subtotal":    invoice_subtotal,
        "tax_rate":            0.0,
        "tax_amount":          0.0,
        "other":               0.0,
        "deposit":             0.0,
        "total":               invoice_subtotal,
        "notes":               "",
        "stale":               False,
        "generated_at":        now,
    }


@app.route("/invoices/<period>/<invoice_id>/diff")
def invoice_diff(period, invoice_id):
    invs = _load_invoices(period)
    inv  = next((i for i in invs if i["invoice_id"] == invoice_id), None)
    if not inv:
        return jsonify({"error": "not found"}), 404
    if not inv.get("stale"):
        return jsonify({"has_changes": False})

    db   = _load_customers()
    live = _compute_live_invoice_data(period, inv["customer_key"], db)

    # Labor diff — key by (date, description)
    def lkey(e): return (e["date"], e.get("description",""))
    stored_lab = {}
    for e in inv.get("labor_entries", []):
        stored_lab.setdefault(lkey(e), []).append(e)
    live_lab = {}
    for e in live["labor_entries"]:
        live_lab.setdefault(lkey(e), []).append(e)

    lab_added, lab_removed, lab_changed = [], [], []
    for k in live_lab:
        if k not in stored_lab:
            lab_added.extend(live_lab[k])
    for k in stored_lab:
        if k not in live_lab:
            lab_removed.extend(stored_lab[k])
    for k in stored_lab:
        if k in live_lab:
            oh = sum(e["hours"] for e in stored_lab[k])
            nh = sum(e["hours"] for e in live_lab[k])
            if abs(oh - nh) > 0.001:
                lab_changed.append({
                    "date": k[0], "description": k[1],
                    "old_hours": oh, "new_hours": nh,
                    "old_amount": sum(e["amount"] for e in stored_lab[k]),
                    "new_amount": sum(e["amount"] for e in live_lab[k]),
                })

    # Material diff — compare as multi-sets of (date, vendor, description, amount) tuples
    def mat_tuple(m): return (m["date"], m.get("vendor",""), m.get("description",""), m["amount"])
    from collections import Counter
    stored_tups = Counter(mat_tuple(m) for m in inv.get("material_entries", []))
    live_tups   = Counter(mat_tuple(m) for m in live["material_entries"])
    mat_added   = [{"date":t[0],"vendor":t[1],"description":t[2],"amount":t[3]}
                   for t,n in (live_tups - stored_tups).items() for _ in range(n)]
    mat_removed = [{"date":t[0],"vendor":t[1],"description":t[2],"amount":t[3]}
                   for t,n in (stored_tups - live_tups).items() for _ in range(n)]

    return jsonify({
        "has_changes":  bool(lab_added or lab_removed or lab_changed or mat_added or mat_removed),
        "labor": {
            "old_total": inv.get("labor_subtotal", 0),
            "new_total": live["labor_subtotal"],
            "added":     lab_added,
            "removed":   lab_removed,
            "changed":   lab_changed,
        },
        "materials": {
            "old_total": inv.get("materials_subtotal", 0),
            "new_total": live["materials_subtotal"],
            "added":     mat_added,
            "removed":   mat_removed,
        },
        "old_total": inv.get("total", 0),
        "new_total": live["total"],
    })


@app.route("/invoices/<period>/<invoice_id>/status", methods=["PUT"])
def update_invoice_status(period, invoice_id):
    body       = request.get_json() or {}
    new_status = body.get("status")
    if new_status not in ("draft", "sent", "paid"):
        return jsonify({"error": "invalid status"}), 400
    invs = _load_invoices(period)
    inv  = next((i for i in invs if i["invoice_id"] == invoice_id), None)
    if not inv:
        return jsonify({"error": "not found"}), 404
    inv["status"] = new_status
    now = datetime.datetime.now().isoformat(timespec="seconds")
    if new_status == "draft":
        inv.pop("sent_at", None)
        inv.pop("paid_at", None)
    elif new_status == "sent":
        if not inv.get("sent_at"):
            inv["sent_at"] = now
        # Invoice date = the date it was actually sent
        today = datetime.date.today().isoformat()
        inv["invoice_date"] = today
        net_days = inv.get("net_days", 20)
        due = datetime.date.today() + datetime.timedelta(days=net_days)
        inv["due_date"] = due.isoformat()
        inv.pop("paid_at", None)   # clear if reverting from paid
    elif new_status == "paid":
        if not inv.get("sent_at"):
            inv["sent_at"] = now   # auto-stamp sent if skipped
        if not inv.get("paid_at"):
            inv["paid_at"] = now
    _save_invoices(period, invs)
    if new_status == "sent":
        _save_invoice_pdf_async(invoice_id, inv, period)
    return jsonify({"ok": True, "invoice": inv})


@app.route("/invoices/<period>/<invoice_id>/email", methods=["POST"])
def email_invoice(period, invoice_id):
    if not SMTP_USER or not SMTP_PASS:
        return jsonify({"error": "Email not configured. Set SMTP_USER and SMTP_PASS environment variables."}), 503
    invs = _load_invoices(period)
    inv  = next((i for i in invs if i["invoice_id"] == invoice_id), None)
    if not inv:
        return jsonify({"error": "Invoice not found"}), 404
    recipient = inv.get("bill_to_email", "").strip()
    if not recipient:
        try:
            db = _load_customers()
            cust = db.get(inv.get("customer_key", ""), {})
            recipient = cust.get("email", "").strip()
            if recipient:
                inv["bill_to_email"] = recipient   # back-fill for future renders
        except Exception:
            pass
    if not recipient:
        return jsonify({"error": "No email address on file for this customer"}), 400

    # Backfill month_label if missing (same logic as print route)
    if not inv.get("month_label"):
        try:
            y, m = int(period.split("_")[0]), int(period.split("_")[1])
            inv["month_label"] = datetime.datetime(y, m, 1).strftime("%B %Y")
        except Exception:
            inv["month_label"] = period

    # Backfill property_label for old invoices generated before the field was added
    if not inv.get("property_label"):
        try:
            db = _load_customers()
            cust = db.get(inv.get("customer_key", ""), {})
            inv["property_label"] = cust.get("property_label", "")
        except Exception:
            pass

    html_body = render_template("invoice_email.html", inv=inv)
    subject   = f"Invoice {invoice_id} – {inv.get('month_label', period)} – Handyman Services"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(EMAIL_FROM, recipient, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(EMAIL_FROM, recipient, msg.as_string())
    except Exception as e:
        return jsonify({"error": f"SMTP error: {str(e)}"}), 500

    # Auto-advance to Sent (if still draft)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    if inv.get("status") == "draft":
        inv["status"]  = "sent"
        inv["sent_at"] = now
    inv["last_emailed_at"] = now
    _save_invoices(period, invs)

    _save_invoice_pdf_async(invoice_id, inv, period)
    return jsonify({"ok": True, "sent_to": recipient, "invoice": inv})


@app.route("/send_message", methods=["POST"])
def send_message():
    if not SMTP_USER or not SMTP_PASS:
        return jsonify({"error": "Email not configured."}), 503
    body    = request.get_json() or {}
    to      = body.get("to", "").strip()
    subject = body.get("subject", "").strip()
    html    = body.get("html", "").strip() or "(no message body)"
    if not to or not subject:
        return jsonify({"error": "Recipient and subject are required."}), 400
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = to
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(EMAIL_FROM, to, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(EMAIL_FROM, to, msg.as_string())
    except Exception as e:
        return jsonify({"error": f"SMTP error: {str(e)}"}), 500
    return jsonify({"ok": True})


@app.route("/invoices/<period>/<invoice_id>", methods=["DELETE"])
def delete_invoice(period, invoice_id):
    invs = _load_invoices(period)
    inv  = next((i for i in invs if i["invoice_id"] == invoice_id), None)
    if not inv:
        return jsonify({"error": "not found"}), 404
    if inv.get("status") != "draft":
        return jsonify({"error": "only draft invoices can be deleted"}), 400
    _save_invoices(period, [i for i in invs if i["invoice_id"] != invoice_id])
    return jsonify({"ok": True})


@app.route("/invoices/<period>/<invoice_id>/print")
def print_invoice(period, invoice_id):
    invs = _load_invoices(period)
    inv  = next((i for i in invs if i["invoice_id"] == invoice_id), None)
    if not inv:
        return "Invoice not found", 404
    # Backfill month_label for invoices generated before this field was added
    if not inv.get("month_label"):
        try:
            y, m = (inv.get("period") or period).split("_")
            inv["month_label"] = datetime.datetime(int(y), int(m), 1).strftime("%B %Y")
        except Exception:
            inv["month_label"] = ""
    # Backfill property_label for old invoices generated before the field was added
    if not inv.get("property_label"):
        try:
            db = _load_customers()
            cust = db.get(inv.get("customer_key", ""), {})
            inv["property_label"] = cust.get("property_label", "")
        except Exception:
            pass
    return render_template("invoice_print.html", inv=inv, period=period)


# ── Tasks ────────────────────────────────────────────────────────────────────

def _load_tasks():
    if not TASKS.exists():
        return []
    return json.loads(TASKS.read_text(encoding="utf-8-sig"))

def _save_tasks(tasks):
    tmp = TASKS.with_suffix(".tmp")
    tmp.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
    os.replace(tmp, TASKS)

def _next_task_id(tasks):
    nums = [int(t["task_id"].split("_")[1]) for t in tasks
            if t.get("task_id", "").startswith("task_") and t["task_id"].split("_")[1].isdigit()]
    return f"task_{(max(nums) + 1) if nums else 1:04d}"

@app.route("/tasks")
def api_tasks():
    return jsonify(_load_tasks())

@app.route("/tasks", methods=["POST"])
def create_task():
    body  = request.get_json() or {}
    tasks = _load_tasks()
    now   = datetime.datetime.now().isoformat(timespec="seconds")
    task  = {
        "task_id":        _next_task_id(tasks),
        "customer_key":   body.get("customer_key", ""),
        "job_label":      body.get("job_label", ""),
        "description":    body.get("description", "").strip(),
        "hours_estimate": body.get("hours_estimate") or None,
        "priority":       body.get("priority", "medium"),
        "due_date":       body.get("due_date") or None,
        "status":         "open",
        "created_at":     now,
        "completed_at":   None,
    }
    tasks.append(task)
    _save_tasks(tasks)
    return jsonify({"ok": True, "task": task})

@app.route("/tasks/<task_id>", methods=["PUT"])
def update_task(task_id):
    body  = request.get_json() or {}
    tasks = _load_tasks()
    task  = next((t for t in tasks if t["task_id"] == task_id), None)
    if not task:
        return jsonify({"error": "not found"}), 404
    for field in ("description", "customer_key", "job_label", "hours_estimate",
                  "priority", "due_date", "status"):
        if field in body:
            task[field] = body[field]
    if task.get("status") == "complete" and not task.get("completed_at"):
        task["completed_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    elif task.get("status") != "complete":
        task["completed_at"] = None
    _save_tasks(tasks)
    return jsonify({"ok": True, "task": task})

@app.route("/tasks/<task_id>", methods=["DELETE"])
def delete_task(task_id):
    tasks = [t for t in _load_tasks() if t["task_id"] != task_id]
    _save_tasks(tasks)
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("\n  Small Jobs - Receipt Manager")
    print("  Local:   http://localhost:5001")
    print("  Network: http://192.168.0.49:5001\n")
    app.run(host='0.0.0.0', debug=True, port=5001, use_reloader=False)
