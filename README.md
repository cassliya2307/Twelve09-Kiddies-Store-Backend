# Twelve’09 Kiddies Store — Backend

Backend API for the Twelve’09 Kiddies Store e-commerce application, built with **Python** and **FastAPI**.

**Live website:** https://twelve09kiddiesstore.com
**Backend repository:** https://github.com/cassliya2307/Twelve09-Kiddies-Store-Backend
**Frontend repository:** https://github.com/cassliya2307/Twelve09-Kiddies-Store-Frontend

---

## Overview

This repository contains the server-side API for a children's-products e-commerce store. It handles authentication, product and category catalog, shopping cart validation and checkout, orders, inventory, addresses, payments (Paystack and bank-transfer receipts), expenses, delivery-fee configuration, admin dashboards, and role-based administration.

The API is designed to be consumed by the Next.js frontend (see Frontend repository link above), which proxies browser requests through a `/api` rewrite to this backend.

---

## Features

Features below are implemented in the source code (verified against `app/`):

- **Authentication & authorization** — JWT (HS256) bearer tokens, password hashing (pbkdf2_sha256), `/auth/register`, `/auth/login`, `/auth/me`. Roles: `CUSTOMER`, `STAFF`, `ADMIN`. Permissions: `MANAGE_PRODUCTS`, `MANAGE_INVENTORY`, `MANAGE_ORDERS`, `MANAGE_CUSTOMERS`, `MANAGE_DELIVERIES`, `MANAGE_EXPENSES`, `VIEW_REPORTS`.
- **Customer accounts** — registration, login, profile (`/auth/me`), order history.
- **Product catalog** — list products (with search, include-inactive toggle), get product by ID.
- **Category management** — list/get categories (public); create categories (admin).
- **Product administration** — create/update/activate/deactivate products, update stock, upload/replace/delete product images (Cloudinary).
- **Homepage categories** — configure featured homepage categories with labels, icons, and display order (admin).
- **Shopping cart validation & checkout** — validate line items and stock, compute subtotal/delivery fee/total, create orders with fulfillment methods (`STORE_DELIVERY`, `CUSTOMER_DISPATCH`, `STORE_PICKUP`). Stock is decremented on order creation and restored on cancellation.
- **Orders** — create, list, get, update status (admin or `MANAGE_ORDERS`), cancel (stock restored), status-transition validation.
- **Addresses** — create/list/get/update/delete user addresses, set default address.
- **Payments** — create payments (`PAYSTACK`, `CARD`, `STRIPE`, `BANK_TRANSFER`, `CASH`), list/get payments, update payment status (admin).
- **Paystack integration** — initialize transaction, verify transaction (returning customer), webhook handler (`charge.success`, `charge.failed`), reclaim-stale admin job for unpaid PENDING Paystack payments.
- **Bank-transfer receipts** — upload a receipt (PDF/JPG/PNG/WEBP, ≤5 MiB, magic-byte validation, stored in DB), retrieve receipt, admin approve/reject a receipt (approving confirms the order).
- **Expenses** — create/list/get/update/delete expenses (admin or `MANAGE_EXPENSES`).
- **Delivery fee** — get/update the delivery-fee configuration (admin).
- **Admin dashboard analytics** — KPIs, sales time series, top products, category performance, inventory snapshot, expense summary, order-status distribution, payment distribution, gross profit (`/admin/dashboard/*`, `VIEW_REPORTS` permission).
- **Admin user management** — list users/customers, update staff permissions and roles (the API rejects promoting a user to `ADMIN` via the role-update endpoint).
- **Health check** — `GET /health`.

### Not implemented / unverified
- No automated administrator registration endpoint exists. The first administrator must be created manually via a direct database operation after deployment (see `.env.example` "Admin Bootstrap").
- Stripe and other non-Paystack payment methods are accepted as enum values but Paystack is the active production gateway.
- Production deployment has not been independently verified by this review.

---

## Technology Stack

Actual dependencies from `requirements.txt` and `requirements-dev.txt`:

| Layer | Technology | Version constraint |
|-------|------------|-------------------|
| Language | Python | 3.13 (per `Dockerfile`: `python:3.13-slim`) |
| Framework | FastAPI | unpinned (`fastapi`) |
| ASGI server | uvicorn | unpinned |
| ORM | SQLAlchemy | unpinned |
| MySQL driver | PyMySQL | unpinned (`pymysql`) |
| PostgreSQL driver | psycopg2-binary | unpinned |
| Settings | pydantic-settings | unpinned |
| Migrations | Alembic | unpinned |
| JWT | PyJWT | unpinned |
| HTTP client | httpx | `>=0.27,<0.29` |
| File uploads | python-multipart | unpinned |
| Images | Pillow | unpinned |
| Image storage | cloudinary | unpinned |
| Email validation | email-validator | unpinned |
| PDF parsing | pypdf | unpinned |
| Environment loading | python-dotenv | unpinned |
| Testing | pytest | `>=8,<9` declared in `requirements-dev.txt`; pytest 9.1.1 was observed installed in this environment, which falls outside that declared range |

Installed versions observed in this environment: Python 3.13.7, FastAPI 0.131.0, SQLAlchemy 2.0.49. Declared constraint for pytest is `>=8,<9` per `requirements-dev.txt`.

---

## Architecture and Project Structure

The application is a single FastAPI app defined in `app/main.py`. It uses SQLAlchemy ORM models, Pydantic schemas, and dependency-injected database sessions.

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app, lifespan, CORS, all route handlers
│   ├── core/
│   │   ├── database.py         # SQLAlchemy engine, Base, Settings (pydantic-settings), get_db
│   │   └── auth.py             # JWT, password hashing, role/permission dependencies
│   ├── api/
│   │   └── admin_dashboard.py  # /admin/dashboard analytics router (VIEW_REPORTS)
│   ├── models/                 # SQLAlchemy models (User, Product, Order, Payment, Address, Category, Expense, DeliveryConfig, HomepageCategoryConfiguration, ...)
│   ├── schemas/                # Pydantic request/response schemas
│   └── services/               # paystack.py, receipt.py, product_image.py, analytics.py
├── alembic/                    # Alembic migrations (MySQL path)
│   ├── env.py
│   └── versions/
├── alembic_pg/                 # Alembic migrations (PostgreSQL path)
│   ├── env.py
│   └── versions/
├── alembic.ini                 # Alembic config (DATABASE_URL from env)
├── alembic_pg.ini              # Alembic config for PostgreSQL path
├── Dockerfile                  # python:3.13-slim, CMD: uvicorn app.main:app
├── railway.toml                # Deploy config: healthcheck /health, preDeploy alembic upgrade head
├── requirements.txt            # Runtime dependencies
├── requirements-dev.txt        # Dev/test dependencies (pytest, httpx)
├── .env.example                # Environment variable template
└── tests/                      # pytest suite (conftest.py forces in-memory SQLite)
```

### Frontend → Backend communication

The Next.js frontend rewrites browser `/api/*` requests to this backend server-side. `CORS_ALLOWED_ORIGINS` controls direct cross-origin browser access; it defaults to the Vercel frontend origin plus localhost origins.

---

## API Overview

| Route group | Endpoints (representative) | Auth | Notes |
|-------------|---------------------------|------|-------|
| Health | `GET /health` | Public | Health check |
| Auth | `POST /auth/register`, `POST /auth/login`, `GET /auth/me` | Public / bearer | Register (customer only), login returns JWT bearer token |
| Users (admin) | `GET /admin/users`, `PATCH /admin/users/{id}/role`, `PATCH /admin/users/{id}/permissions` | Admin | Cannot promote a user to `ADMIN` |
| Customers | `GET /admin/customers`, `GET /admin/customers/{id}/orders` | `MANAGE_CUSTOMERS` | |
| Products | `GET /products`, `GET /products/{id}`, `POST /products`, `PUT /products/{id}`, `PATCH /products/{id}/stock|activate|deactivate`, `POST /products/{id}/upload-image`, `DELETE /products/{id}/image` | Public read / admin or permission write | Image upload uses Cloudinary |
| Categories | `GET /categories`, `GET /categories/{id}`, `POST /categories` | Public read / admin write | |
| Homepage categories | `GET /homepage-categories`, `GET|PUT /admin/homepage-categories` | Public read / admin write | |
| Cart/Checkout | `POST /checkout/validate`, `POST /checkout` | Bearer | Validates stock; creates order |
| Orders | `GET /orders`, `GET /orders/{id}`, `POST /orders`, `PATCH /orders/{id}/status`, `POST /orders/{id}/cancel` | Bearer / admin-or-permission | Status transitions validated; cancellation restores stock |
| Addresses | `GET|POST /addresses`, `GET|PUT|DELETE /addresses/{id}`, `PATCH /addresses/{id}/default` | Bearer (owner) | |
| Payments | `POST /payments`, `GET /payments`, `GET /payments/{id}`, `GET /orders/{id}/payments`, `PATCH /payments/{id}/status` | Bearer / admin | Methods: PAYSTACK, CARD, STRIPE, BANK_TRANSFER, CASH |
| Receipts | `POST /payments/{id}/receipt`, `GET /payments/{id}/receipt`, `POST /payments/{id}/receipt/approve|reject` | Bearer owner / admin | Bank-transfer receipt review |
| Paystack | `POST /payments/paystack/initialize`, `POST /payments/paystack/verify`, `POST /webhooks/paystack`, `POST /admin/payments/reclaim-stale` | Bearer / admin | Initialize, verify, webhook, stale-payment reclaim |
| Delivery fee | `GET /admin/delivery-fee`, `PATCH /admin/delivery-fee` | Admin | |
| Expenses | `GET|POST /expenses`, `GET|PATCH|DELETE /expenses/{id}` | Bearer / admin | |
| Admin dashboard | `GET /admin/dashboard/{kpis,sales,top-products,categories,inventory,expenses,orders/status,payments,gross-profit}` | `VIEW_REPORTS` | Analytics |

Local interactive docs: `GET /docs` and `GET /redoc` are enabled only when `ENVIRONMENT=development`; they are disabled in production.

---

## Authentication and Authorization

- **Token:** JWT (HS256) created by `create_access_token(user_id)`, expires after `ACCESS_TOKEN_EXPIRE_MINUTES` (default 60). Passed as `Authorization: Bearer <token>`. Token URL: `/auth/login`.
- **Password:** hashed with `pbkdf2_sha256$<salt>$<digest>` (200,000 iterations) via `hash_password` / `verify_password`.
- **Roles:** `CUSTOMER` (default on registration), `STAFF`, `ADMIN`. `ADMIN` is granted implicitly by `require_admin`; STAFF users hold explicit `Permission` entries.
- **Dependencies:** `get_current_user` (bearer), `require_admin`, `require_permission(permission)`, `permission_required(permission)`.
- **Admin bootstrap:** There is **no automated admin registration endpoint**. The first administrator must be created manually via direct database SQL after deployment (promote a user to `role='ADMIN'`). The API rejects promotion to `ADMIN` via the role-update endpoint.
- **Receipt approval/rejection:** `POST /payments/{id}/receipt/approve` and `/reject` are admin-only (`require_admin`). Approving sets payment `SUCCESS` and confirms a `PENDING` order; rejecting sets payment `FAILED` and leaves the order `PENDING`.

> Never commit real `SECRET_KEY`, Paystack keys, or database credentials. Use `.env` (gitignored) with placeholder values locally.

---

## Database and Migrations

- **ORM:** SQLAlchemy 2.x declarative base (`app/core/database.py:Base`).
- **Dialects:** MySQL via `mysql+pymysql://` (default in `.env.example`), PostgreSQL via `postgresql+psycopg2://`, SQLite via `sqlite:///` (used by tests). `normalize_database_url()` rewrites generic `mysql://`/`postgres://` schemes to the correct driver form.
- **Configuration:** `DATABASE_URL` (required) is read from `.env` via pydantic-settings `Settings`.
- **Migrations:** Alembic. Two migration paths exist:
  - `alembic/` + `alembic.ini` (MySQL/default path)
  - `alembic_pg/` + `alembic_pg.ini` (PostgreSQL path)
  - `alembic env.py` reads `settings.DATABASE_URL` and uses `Base.metadata` as `target_metadata`.
- **Local migration commands (development only; back up data and verify your target database before applying):**
  ```bash
  # Show current migration head
  alembic current

  # Generate a new migration after model changes
  alembic revision --autogenerate -m "describe changes"

  # Apply migrations to the configured database
  alembic upgrade head
  ```
  Use `alembic -c alembic_pg.ini` for the PostgreSQL path. Do not run migrations against production without a backup.

---

## Prerequisites and Local Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/cassliya2307/Twelve09-Kiddies-Store-Backend.git
   cd Twelve09-Kiddies-Store-Backend
   ```

2. **Create and activate a virtual environment**

   Unix/macOS:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   Windows PowerShell:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   # For testing:
   pip install -r requirements-dev.txt
   ```

4. **Create a local environment file**
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` with your values (see Environment Variables below). `.env` is gitignored and must not be committed.

5. **Configure a local development database**
   Set `DATABASE_URL` in `.env` to a database you own. For a quick local trial without a database server, tests use an in-memory SQLite database, but running the application locally requires a real `DATABASE_URL` (MySQL or PostgreSQL per `DATABASE_URL`).

6. **Apply migrations (local development warning)**
   ```bash
   alembic upgrade head
   ```
   **Warning:** This modifies your target database. Back up data and verify you are pointing at a local/development database, not production.

7. **Start the development server**
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```
   (Production/Docker startup per `Dockerfile`: `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1`)

8. **Open the API docs** (development only)
   - Swagger UI: http://localhost:8000/docs
   - ReDoc: http://localhost:8000/redoc

---

## Environment Variables

Exact variable names from `app/core/database.py` (`Settings`) and `.env.example`. Use placeholders; do not reveal real values.

| Variable | Required | Default | Secret? | Purpose |
|----------|----------|---------|---------|---------|
| `DATABASE_URL` | Yes | — | Yes | Database connection URL (MySQL/PostgreSQL/SQLite) |
| `SECRET_KEY` | Yes | — | Yes | JWT signing key; generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `JWT_ALGORITHM` | No | `HS256` | No | JWT algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | No | `60` | No | Access token lifetime in minutes |
| `CLOUDINARY_CLOUD_NAME` | No | `""` | Yes | Cloudinary cloud name (production image uploads) |
| `CLOUDINARY_API_KEY` | No | `""` | Yes | Cloudinary API key |
| `CLOUDINARY_API_SECRET` | No | `""` | Yes | Cloudinary API secret |
| `PAYSTACK_SECRET_KEY` | No* | `""` | Yes | Paystack secret key (required for live payments) |
| `PAYSTACK_WEBHOOK_SECRET` | No* | `""` | Yes | Paystack webhook signature secret |
| `PAYSTACK_PUBLIC_KEY` | No | (omitted) | No | Not required by released code; test fixtures only |
| `PRODUCT_IMAGE_UPLOAD_DIR` | No | `static/uploads` | No | Local uploads directory (legacy) |
| `PRODUCT_IMAGE_MAX_SIZE` | No | `5242880` | No | Max upload size in bytes (5 MB) |
| `DB_POOL_SIZE` | No | `5` | No | DB connection pool size |
| `DB_MAX_OVERFLOW` | No | `10` | No | DB pool max overflow |
| `DB_POOL_RECYCLE` | No | `3600` | No | DB pool recycle seconds |
| `DB_POOL_TIMEOUT` | No | `30` | No | DB pool timeout seconds |
| `CORS_ALLOWED_ORIGINS` | No | frontend + localhost origins | No | Comma-separated allowed CORS origins |
| `CORS_ALLOW_CREDENTIALS` | No | `true` | No | CORS credentials flag |
| `ENVIRONMENT` | No | `development` | No | `development` enables /docs and /redoc; `production` disables them |

\* `PAYSTACK_SECRET_KEY` and `PAYSTACK_WEBHOOK_SECRET` are required for production Paystack payments/webhooks.

---

## Testing and Code Quality

- **Framework:** pytest (`>=8,<9`, declared in `requirements-dev.txt`). `httpx` is also required by `fastapi.testclient.TestClient`.
- **Test database:** `tests/conftest.py` forces `DATABASE_URL=sqlite://` (in-memory SQLite) and `SECRET_KEY` to a test-only value before importing the app, so tests run isolated from production.
- **PostgreSQL integration tests** (`test_pg_target_smoke.py`, `test_receipt_workflow.py`) are opt-in: they only run when both `RUN_OPT_IN=1` and `APPROVAL_OPT_IN=1` are set.
- **Run the full suite:**
  ```bash
  python -m pytest tests/ -q
  ```
- **Run a single test file:**
  ```bash
  python -m pytest tests/test_business_rules.py -q
  ```

### Observed results (this repository)

Running the safe (non-PostgreSQL, non-network) test files (`test_business_rules.py`, `test_cancel_inventory.py`, `test_database_url_normalization.py`, `test_homepage_category_configuration.py`) yielded:

```
142 passed, 16 failed in ~74s
```

The 16 failures are in image-upload/Cloudinary-related tests; they require Cloudinary configuration / network access not present in this environment. They are reported here as observed failures in this run. No baseline status is established, and no claim is made that the full suite passes; run the suite yourself in your environment.

There is no separate lint or type-check command configured in the repository.

---

## Deployment

- **Container:** `Dockerfile` (`python:3.13-slim`). Installs `gcc`, `libmariadb-dev`, `libpq-dev` (for PyMySQL/psycopg2), copies `requirements.txt`, `app/`, `alembic/`, `alembic_pg/` and configs.
- **Startup command** (from `Dockerfile`):
  ```bash
  uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
  ```
  The container exposes port `8000`.
- **Deploy config present:** `railway.toml` specifies:
  - `healthcheckPath = "/health"`
  - `healthcheckTimeout = 100`
  - `preDeployCommand = "alembic upgrade head"`
- **Health-check endpoint:** `GET /health` returns `{"status": "ok", "message": "Twelve09 API is running"}`.
- **Required production environment variables:** `DATABASE_URL`, `SECRET_KEY`, `CLOUDINARY_*` (for image uploads), `PAYSTACK_SECRET_KEY`, `PAYSTACK_WEBHOOK_SECRET`, `CORS_ALLOWED_ORIGINS`, `ENVIRONMENT=production`.
- **Migrations on deploy:** `preDeployCommand = "alembic upgrade head"` runs Alembic migrations before deploy. Verify the target database and back up data before changing production migration behavior.
- **Hosting target:** The repository contains a `Dockerfile` and `railway.toml` (Railway deploy configuration). Render is referenced in project context as a hosting arrangement but is not independently verifiable from the repository files themselves. Configure whichever platform you use with the environment variables above and the Docker startup command. Do not make production configuration changes from this README.

---

## Known Limitations and Development Notes

- No automated admin registration endpoint; the first `ADMIN` must be created manually via direct database SQL (documented in `.env.example` "Admin Bootstrap").
- The API explicitly rejects promoting a user to `ADMIN` via the role-update endpoint (a single-admin assumption is suggested by the error message text, but not independently enforced in code).
- The 16 image-upload test failures are pre-existing and relate to Cloudinary/network availability; they are not caused by this documentation.
- Stripe and other non-Paystack payment methods are accepted as enum values but Paystack is the active gateway; non-Paystack flows are unverified by this review.
- Production deployment and live-site behavior have not been independently verified by this review.
- `.env` and `.env.*` are gitignored; create them locally from `.env.example`.
- Pydantic V1-style `@validator` and class-based `Config` are used in some schemas (deprecation warnings under Pydantic V2); these are pre-existing code-style items, not functional defects.

---

## Contributing

Contributions are welcome. Open an issue or pull request against the backend repository. Before submitting, run the relevant tests with `python -m pytest tests/ -q` and ensure you are not committing `.env` or any secret values. Keep changes consistent with the existing FastAPI/SQLAlchemy/Alembic structure and the role/permission authorization model.

---

## License

No license file was identified in this repository during this review. Do not assume redistribution or usage rights without permission from the repository owner.
