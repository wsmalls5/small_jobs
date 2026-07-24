# Small Jobs

Handyman business management app — receipts, expenses, hours, invoices, and tasks.
Built with Python/Flask. Runs locally at `http://localhost:5001`.

---

## Setup

### 1. Prerequisites

| Tool | Windows | Mac |
|------|---------|-----|
| Python 3.10+ | [python.org](https://www.python.org/downloads/) | `brew install python` |
| Tesseract OCR | [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki) | `brew install tesseract` |
| Dropbox | [dropbox.com](https://www.dropbox.com/install) | [dropbox.com](https://www.dropbox.com/install) |

> **Mac only:** If you don't have Homebrew: `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`

### 2. Clone and install

```bash
git clone https://github.com/wsmalls5/small_jobs.git
cd small_jobs
pip install -r requirements.txt
```

### 3. Create your `.env` file

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env    # Mac
copy .env.example .env  # Windows
```

Key values to set:

```
# Email — for sending invoices (optional)
SMTP_USER=your-email@gmail.com
SMTP_PASS=your-16-char-app-password

# Dropbox receipt folders
RECEIPT_INBOX=/Users/yourname/Dropbox/Small Jobs/Inbox       # Mac
RECEIPT_REVIEWED=/Users/yourname/Dropbox/Small Jobs/Reviewed # Mac
```

> Gmail App Passwords: Google Account → Security → 2-Step Verification → App Passwords

### 4. Run

```bash
python scripts/small_jobs.py
```

Open `http://localhost:5001` in your browser.

---

## Data

All business data lives in `data/` (git-ignored — never committed).
Copy the `data/` folder from another machine to carry over existing records,
or start fresh — the app creates the folder structure automatically on first run.

---

## Folder structure

```
small_jobs/
├── scripts/
│   ├── small_jobs.py        # main app
│   ├── static/              # CSS, JS, icons
│   └── templates/           # HTML templates
├── data/                    # git-ignored — local business data
├── .env                     # git-ignored — local credentials
├── .env.example             # safe to commit — template only
└── requirements.txt
```
