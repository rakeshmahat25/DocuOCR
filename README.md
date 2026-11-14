# DocuOCR

**B2B SaaS platform for document OCR, AI-powered data extraction, validation, and fraud detection.**

DocuOCR lets organizations upload invoices, identity documents, bank records, certificates, and many other document types, then automatically extracts structured fields using Gemini or a built-in local fallback, validates the results against custom rules, flags suspicious files, and routes low-confidence records to a human review dashboard.

---

## Table of Contents

- [Key Features](#key-features)
- [Tech Stack](#tech-stack)
- [Architecture](#architecture)
- [Processing Pipeline](#processing-pipeline)
- [Supported Document Types](#supported-document-types)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
- [Environment Variables](#environment-variables)
- [Usage](#usage)
- [REST API](#rest-api)
- [Webhooks](#webhooks)
- [WebSockets](#websockets)
- [Data Model](#data-model)
- [Testing](#testing)
- [Deployment Notes](#deployment-notes)
- [Limitations & Roadmap](#limitations--roadmap)

---

## Key Features

- **Multi-tenant SaaS core** — Organizations (Tenants) contain multiple Workspaces, with membership roles for Admin, Reviewer, Operator, Auditor, and Client.
- **Custom extraction workflows** — Define document types, target fields, data types, required flags, and validation rules per workspace. A default workflow is auto-generated when none is selected.
- **Hybrid AI extraction** — Multimodal extraction with Google Gemini 2.5 Flash, with a deterministic regex-based local extractor as a zero-cost fallback.
- **OCR and preprocessing** — PDF rasterization with PyMuPDF, EXIF-aware rotation, contrast/sharpness enhancement, Tesseract OCR, and PaddleOCR support for Nepali/Devanagari text.
- **Automatic document classification** — Detect 40+ document types from filenames when no workflow is specified.
- **Rule engine** — Required-field checks, regex validation, and safe arithmetic expressions (for example `subtotal + tax_amount == total_amount`).
- **Fraud detection** — Duplicate file hashes, PDF editing-software metadata signatures, post-creation revision mismatches, and required-signature checks.
- **Human-in-the-loop review** — Side-by-side page preview and editable extracted fields, with corrections saved and re-validated before completion.
- **Async processing** — Background-thread fallback out of the box; optional Celery + Redis workers.
- **Realtime updates** — Django Channels WebSocket notifications per workspace.
- **Webhook integrations** — Signed HMAC-SHA256 deliveries with delivery logs and event subscriptions.
- **Audit trail & analytics** — Immutable audit entries, workspace statistics, category breakdowns, and full-text search across filenames and OCR text.
- **Responsive UI** — Django templates styled with Tailwind CSS, dark/light themes, and a collapsible workspace-aware sidebar.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Django 5.0, Django REST Framework |
| Realtime | Django Channels 4, Daphne (ASGI) |
| Task queue | Celery 5 + Redis (optional; thread fallback built in) |
| AI extraction | Google Gemini (`google-generativeai`), local regex extractor |
| OCR | Tesseract, PaddleOCR (Nepali + English) |
| Document handling | PyMuPDF (PDF), Pillow / pillow-heif (images), python-docx |
| Database | SQLite (default development database) |
| Frontend | Django templates, Tailwind CSS 3 |
| Auth | Django sessions, organization API keys, SimpleJWT (configured) |

---

## Architecture

DocuOCR is a modular Django project composed of four applications plus the project package.

```text
Browser / SDK clients
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│ docuocr/  (settings, ASGI, WSGI, Celery, root URLs)     │
└─────────────────────────────────────────────────────────┘
        │
        ├── core/        Tenants, Workspaces, Users, plans,
        │                onboarding, auth, tenant middleware
        ├── documents/   Workflows, extraction pipeline, OCR,
        │                preprocessing, fraud checks, review UI
        ├── api/         REST API v1, API-key auth, webhooks
        └── analytics/   Audit logs, usage metrics, search
```

| App | Responsibility |
|---|---|
| `core` | Custom user model, tenant/workspace/membership models, subscriptions, onboarding, login, dashboard, team management, `TenantMiddleware`, workspace context processor |
| `documents` | `Workflow` / `ExtractionField` definitions, upload and review views, preprocessing, OCR, Gemini + local extraction, validation engine, fraud detection, WebSocket consumer |
| `api` | DRF endpoints for upload/detail/verify, `X-Organization-API-Key` authentication, webhook endpoints and HMAC dispatcher, integration guide page |
| `analytics` | `AuditLog`, `UsageMetric`, analytics dashboard, global OCR text search |

### Request / workspace resolution

`core.middleware.TenantMiddleware` resolves the active workspace for every request:

1. `X-Workspace-ID` header, if present, selects the workspace directly.
2. Otherwise, for authenticated users, the workspace stored in `request.session["active_workspace_id"]` is used.
3. If no session workspace exists, the user's first `WorkspaceMember` record is selected and persisted in the session.

The resolved objects are exposed as `request.workspace` and `request.tenant`, and to templates via `core.context_processors.workspace_context`.

---

## Processing Pipeline

`documents/ai_pipeline.py::process_document` orchestrates the full lifecycle. A `Document` moves through these statuses:

```text
pending → preprocessing → extracting → validating → human_review → completed
                                                   ↘ failed
```

1. **Preprocessing**
   - PDFs are rasterized at 150 DPI with PyMuPDF. Embedded text is extracted per page.
   - Images are EXIF-rotated, converted to RGB, and enhanced for contrast and sharpness (HEIC/HEIF supported).
   - Each processed page is stored as a `DocumentPage`, and consolidated text is saved to `Document.raw_text`.

2. **Workflow resolution**
   - If no workflow was selected, the document type is inferred from filename keywords.
   - An existing active workflow for that type is reused, or a default workflow with standard fields is created automatically.

3. **Extraction**
   - **Gemini path:** If a workspace-level or global Gemini API key is available, page images and a structured field schema are sent to `models/gemini-2.5-flash`. The response is parsed as JSON with fields, confidence scores, and optional bounding boxes.
   - **Local fallback path:** Regex extractors derive common fields (invoice numbers, dates, amounts, names, IDs, and more) from OCR text. No external API is required.

4. **Validation**
   - Required fields must be present.
   - Optional regex rules validate formats.
   - Safe arithmetic expressions validate totals. Expressions are token-filtered and evaluated without builtins.
   - Results are persisted as `DocumentFieldResult` rows with per-field confidence and messages.

5. **Fraud checks**
   - Duplicate file hash within the workspace.
   - Editing tools in PDF metadata (Photoshop, Illustrator, GIMP, Inkscape, etc.).
   - Creation vs. modification metadata mismatch.
   - Required signature detection for contracts/agreements.

6. **Routing**
   - Documents with confidence below `0.85`, validation warnings/failures, or fraud flags go to `human_review`.
   - Otherwise they are marked `completed`.
   - Realtime and webhook notifications are dispatched.

### Async execution

`trigger_document_processing()` starts a daemon thread by default, so processing works without any worker infrastructure. A Celery task (`documents.tasks.process_document_task`) is also available for production deployments. Webhook delivery prefers Celery and falls back to a background thread.

---

## Supported Document Types

The auto-classifier and default workflow factory recognize the following categories (40+ types):

- **Nepali identity & government:** citizenship, passport, voter ID, PAN, driving license, land ownership (Lal Mohan), vehicle registration, birth certificate, marriage certificate, tax certificate, permits/licenses
- **Banking & finance:** bank statements, cheques, deposit slips, loan agreements, credit card statements, bank certificates, transaction receipts
- **Education:** transcripts, certificates, admit cards, degrees, character certificates, recommendation letters, ID cards, registration certificates
- **Commercial:** invoices, receipts, contracts/agreements, insurance documents
- **HR:** payslips, offer letters, resumes
- **Other:** utility bills, medical records, generic KYC IDs, custom documents

Each type ships with a sensible default field set, including Nepali-specific fields such as Lal Mohan number, Kitta number, and Bikram Sambat date fields.

---

## Project Structure

```text
DocuOCR/
├── manage.py
├── requirements.txt
├── package.json                 # Tailwind build scripts
├── tailwind.config.js
├── db.sqlite3                   # local development database (gitignored)
├── docuocr/
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py                  # HTTP + WebSocket routing
│   ├── wsgi.py
│   └── celery.py
├── core/
│   ├── models.py                # User, Tenant, Workspace, membership, subscriptions
│   ├── views.py                 # onboarding, auth, dashboard, team settings
│   ├── middleware.py            # tenant/workspace resolution
│   ├── context_processors.py
│   └── urls.py
├── documents/
│   ├── models.py                # Workflow, ExtractionField, Document, pages, results
│   ├── views.py                 # workflow CRUD, upload, review, text extraction
│   ├── ai_pipeline.py           # classification, extraction, validation, routing
│   ├── preprocessing.py
│   ├── ocr.py                   # Tesseract + PaddleOCR helpers
│   ├── fraud_detector.py
│   ├── tasks.py
│   ├── consumers.py             # workspace WebSocket consumer
│   └── urls.py
├── api/
│   ├── views.py                 # REST endpoints + integration guide
│   ├── authentication.py        # organization API key auth
│   ├── models.py                # webhook endpoints + delivery logs
│   ├── tasks.py                 # signed webhook dispatcher
│   └── urls.py
├── analytics/
│   ├── models.py                # AuditLog, UsageMetric
│   ├── views.py                 # dashboards and search
│   └── urls.py
├── templates/                   # base layout, dashboards, auth, review UI
├── static/css/src/theme.css     # Tailwind source
├── static/css/dist/styles.css   # compiled CSS
└── media/                       # uploaded documents, page images, temp OCR output
```

---

## Getting Started

### Prerequisites

- **Python 3.10+** (developed against Python 3.12)
- **Node.js 18+ and npm** (only needed to rebuild Tailwind CSS)
- **Tesseract OCR** — required for image OCR
- **Redis** — optional, only for Celery/Celery-based webhook delivery
- **Google Gemini API key** — optional; the local extractor works without it

#### Install Tesseract

- **Windows:** install from the [UB Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki). The app auto-detects `C:\Program Files\Tesseract-OCR\tesseract.exe` or `C:\Program Files (x86)\Tesseract-OCR\tesseract.exe`; otherwise add `tesseract` to `PATH`.
- **macOS:** `brew install tesseract`
- **Debian/Ubuntu:** `sudo apt install tesseract-ocr`

PaddleOCR models for Nepali/English are downloaded on first use of the text extraction page.

### 1. Clone and create a virtual environment

```bash
git clone https://github.com/rakeshmahat25/DocuOCR
cd DocuOCR

python -m venv venv

# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

> **Note:** `paddlepaddle` and `paddleocr` are large packages. If you only need the core document pipeline (Tesseract + Gemini/local extraction), you can install the remaining dependencies and skip PaddleOCR; only the Nepali/English text extraction page depends on it.

### 3. Install frontend dependencies and build CSS

```bash
npm install
npm run build:css
```

A precompiled stylesheet already exists at `static/css/dist/styles.css`, so this step is only necessary after changing Tailwind classes or `theme.css`.

For development with live rebuilds:

```bash
npm run watch:css
```

### 4. Configure environment variables

Create a `.env` file in the project root:

```dotenv
SECRET_KEY=replace-with-a-long-random-string
DEBUG=True
GEMINI_API_KEY=your-google-gemini-api-key
CELERY_BROKER_URL=redis://localhost:6379/0
```

All variables are optional for local development except a production `SECRET_KEY`.

### 5. Apply migrations and create an admin

```bash
python manage.py migrate
python manage.py createsuperuser
```

### 6. Run the application

```bash
python manage.py runserver
```

Because `daphne` is installed and listed first in `INSTALLED_APPS`, `runserver` serves the ASGI application, enabling WebSocket support during development.

Open <http://127.0.0.1:8000/>. You will be redirected to the dashboard, and then to login/signup if you are not authenticated.

### 7. Optional: run Celery and Redis

Document processing works via background threads without Celery. To use Celery for tasks and webhook delivery:

```bash
redis-server
celery -A docuocr worker -l info
```

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | insecure development key | Django cryptographic signing key — **set in production** |
| `DEBUG` | `True` | Set to `False` in production |
| `GEMINI_API_KEY` | unset | Global Gemini API key for AI extraction; can be overridden per tenant |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Celery broker connection |

`ALLOWED_HOSTS` is currently `["*"]` and must be restricted for production use.

---

## Usage

1. **Sign up** at `/signup/` — creates a `Tenant`, an admin `User`, a default `Workspace`, membership, and a Free subscription.
2. **Upload documents** from the dashboard. Supported formats: PDF, PNG, JPG/JPEG, TIFF, HEIC.
3. **Watch processing** — status updates stream into the dashboard over WebSocket.
4. **Review low-confidence documents** at `/documents/<id>/review/` — compare source pages with extracted fields, edit values, and submit. Submission re-runs validation, records an audit log, and completes the document.
5. **Configure workflows** at `/documents/workflows/` — choose a document type, add extraction fields, mark them required, and provide a custom LLM instruction override.
6. **Integrate** at `/api/integrations/` — copy generated cURL/Python/Node snippets, manage webhook endpoints, and inspect delivery logs.
7. **Analyze** at `/analytics/dashboard/`, and search OCR text globally with `/analytics/search/?q=...`.
8. **Extract plain text** at `/documents/text-extract/` — upload a PDF, DOCX, or image and download the extracted text as a DOCX file.

### Main routes

| Route | Description |
|---|---|
| `/signup/`, `/login/`, `/logout/` | Onboarding and authentication |
| `/dashboard/` | Workspace dashboard and document list |
| `/documents/upload/` | AJAX document upload endpoint |
| `/documents/workflows/` | Workflow list / create / edit / delete |
| `/documents/<id>/review/` | Human review dashboard |
| `/documents/text-extract/` | Nepali/English text extraction |
| `/api/integrations/` | API keys, code snippets, webhook console |
| `/analytics/dashboard/` | Usage and audit analytics |
| `/analytics/search/` | Search across document names and OCR text |
| `/admin/` | Django admin |
| `/api/v1/...` | REST API v1 |

---

## REST API

Authenticate with the organization API key in the `X-Organization-API-Key` header. The key is available on the integration page and is stored as `Tenant.api_key`.

### Upload a document

```http
POST /api/v1/documents/upload/
Content-Type: multipart/form-data
X-Organization-API-Key: <tenant-api-key>
```

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | yes | PDF, PNG, JPG/JPEG, or TIFF |
| `workflow_id` | integer | no | Workspace workflow to use; auto-detected if omitted |

```bash
curl -X POST http://127.0.0.1:8000/api/v1/documents/upload/ \
  -H "X-Organization-API-Key: <tenant-api-key>" \
  -F "file=@invoice.pdf" \
  -F "workflow_id=1"
```

**Response `201 Created`**

```json
{
  "message": "File uploaded and enqueued successfully.",
  "document_id": 42,
  "status": "pending",
  "file_type": "pdf"
}
```

### Get document results

```http
GET /api/v1/documents/<document_id>/
X-Organization-API-Key: <tenant-api-key>
```

Returns document status, confidence, validation and fraud status, per-field results, and raw OCR text.

### Submit corrections / verify

```http
POST /api/v1/documents/<document_id>/verify/
Content-Type: application/json
X-Organization-API-Key: <tenant-api-key>

{
  "fields": {
    "invoice_number": "INV-2026-001",
    "total_amount": "1250.00"
  }
}
```

Corrections are saved, validation rules are re-run, and the document is marked completed.

### Authentication modes

| Mode | Mechanism |
|---|---|
| Organization API key | `X-Organization-API-Key` header; resolves the tenant's primary workspace and an admin actor |
| Session | Standard Django session for browser users |
| JWT | `rest_framework_simplejwt` is configured in DRF settings; token routes are not exposed yet |

The active workspace can also be selected with the `X-Workspace-ID` header.

---

## Webhooks

Register endpoints per workspace from `/api/integrations/`. Supported events:

- `document.processed`
- `document.failed`
- `document.fraud_alert`

Each endpoint receives an HMAC-SHA256 signature:

```text
X-DocuOCR-Signature: <hex digest of raw JSON body using endpoint secret>
```

Payload shape:

```json
{
  "event": "document.processed",
  "document_id": 42,
  "name": "invoice.pdf",
  "status": "completed",
  "file_type": "pdf",
  "confidence_score": 0.94,
  "extracted_json": { "...": "..." },
  "validation_status": "verified",
  "fraud_status": "passed",
  "timestamp": "2026-01-01T12:00:00+00:00"
}
```

Every delivery attempt is recorded in `WebhookDeliveryLog` with status code, response body (capped at 2 KB), and timestamp. Delivery failures use status `599` internally.

---

## WebSockets

Authenticated browser clients can subscribe to workspace updates:

```text
ws://127.0.0.1:8000/ws/documents/<workspace_id>/
```

The connection is rejected for unauthenticated users. Events include `document.processed` and `document.removed`, with the document's status, validation status, fraud status, and confidence.

The default channel layer is `InMemoryChannelLayer`. For multiple processes or production, switch `CHANNEL_LAYERS` to `channels_redis` (already in `requirements.txt`).

---

## Data Model

### Core

- **User** — custom `AbstractUser` with role (`admin`, `reviewer`, `operator`, `auditor`, `client`) and optional MFA flag.
- **Tenant** — organization with unique subdomain and organization API key; optional per-tenant Gemini key.
- **Workspace** — operational container inside a tenant.
- **WorkspaceMember** — user/workspace membership with role; unique per pair.
- **SubscriptionPlan / Subscription** — plan limits and tenant billing state.

### Documents

- **Workflow** — tenant/workspace-scoped extraction template: document type and optional system prompt override.
- **ExtractionField** — field key, label, data type (`text`, `number`, `date`, `boolean`, `table`), required flag, and validation rules.
- **Document** — uploaded file with `status`, `validation_status`, `fraud_status`, `extracted_json`, `raw_text`, confidence, reviewer, and timestamps.
- **DocumentPage** — rendered page image, OCR text, and confidence.
- **DocumentFieldResult** — extracted vs. corrected value, confidence, validation state/message, and bounding box.
- **FraudCheck** — individual rule result with risk level and description.

### API & Analytics

- **WebhookEndpoint** — URL, HMAC secret, subscribed events, active flag.
- **WebhookDeliveryLog** — payload, response code/body, sent timestamp.
- **AuditLog** — workspace, actor, action, JSON details, IP address.
- **UsageMetric** — per-tenant daily document count, processing time, and API calls.

---

## Testing

```bash
python manage.py test
```

The test suite in `documents/tests.py` covers:

- Tenant onboarding and relationships
- Arithmetic validation rules (pass/fail cases)
- End-to-end document pipeline with the local extractor
- REST upload endpoint with organization API key headers

During tests, async processing and webhook dispatch run synchronously (or are skipped) to avoid threading and SQLite locking issues.

---

## Deployment Notes

- Set `DEBUG=False` and a secure `SECRET_KEY`.
- Replace the default SQLite database with PostgreSQL (or another production database) in `settings.py`.
- Restrict `ALLOWED_HOSTS` and configure `CSRF_TRUSTED_ORIGINS`.
- Serve static files via WhiteNoise (already configured) and media files via object storage or a reverse proxy.
- Use `channels_redis` for `CHANNEL_LAYERS` and Redis/Celery for background work.
- Run Daphne or another ASGI server (not `runserver`) behind a reverse proxy.
- Proxy request bodies and configure timeouts for large PDFs.
- Store uploaded documents and page images in durable, access-controlled storage.

---

## Limitations & Roadmap

- The default database is SQLite; multi-worker deployments should move to PostgreSQL.
- The in-memory channel layer does not work across processes.
- The local extraction fallback is regex-based and intentionally conservative; it is not a substitute for Gemini on complex layouts.
- PDF OCR fallback is only used by the standalone text-extraction page. The main pipeline relies on embedded PDF text plus image-based Gemini extraction; image OCR requires Tesseract.
- Fraud detection covers metadata and duplicate checks; it is not a full forensic document authentication system.
- Subscription and usage models exist, but payment gateway integration is not implemented.
- JWT authentication is configured but token issue/refresh routes are not yet exposed.
- The analytics dashboard uses some illustrative performance values rather than persisted latency metrics.

Potential next steps: payment/billing integration, PostgreSQL migration, per-tenant rate limiting, background task monitoring, OCR language packs, and a documented OpenAPI schema.

---

## License

No license file is currently included. Add one before distributing or open-sourcing the project.
