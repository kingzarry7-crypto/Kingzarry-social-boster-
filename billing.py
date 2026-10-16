import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/billing", tags=["billing"])
APP_URL = os.getenv("APP_URL", "https://social-booster-api-production.up.railway.app").rstrip("/")
DATABASE_PATH = os.getenv("DATABASE_PATH", str(Path(__file__).resolve().parent / "social_booster.db"))
PLANS = {
    "free": {"name": "Free", "amount": 0, "currency": "NGN", "period_days": 0, "features": ["10 AI generations/month", "3 profile audits/month", "Save drafts and plan campaigns"]},
    "creator": {"name": "Creator", "amount": 3000, "currency": "NGN", "period_days": 30, "features": ["100 AI generations/month", "25 profile audits/month", "30-day access"]},
    "pro": {"name": "Pro", "amount": 8000, "currency": "NGN", "period_days": 30, "features": ["500 AI generations/month", "100 profile audits/month", "30-day access"]},
    "agency": {"name": "Agency", "amount": 25000, "currency": "NGN", "period_days": 30, "features": ["2,000 AI generations/month", "500 profile audits/month", "30-day access"]},
}

def db():
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn

def init_billing_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS billing_payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL, email TEXT NOT NULL, plan TEXT NOT NULL,
            amount INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'NGN',
            status TEXT NOT NULL DEFAULT 'pending', provider_transaction_id TEXT,
            created_at TEXT NOT NULL, paid_at TEXT
        );
        CREATE TABLE IF NOT EXISTS billing_usage(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            feature TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_billing_usage_user_feature_date ON billing_usage(user_id,feature,created_at);
        CREATE TABLE IF NOT EXISTS billing_subscriptions(
            user_id INTEGER PRIMARY KEY, plan TEXT NOT NULL DEFAULT 'free',
            status TEXT NOT NULL DEFAULT 'free', starts_at TEXT, expires_at TEXT,
            updated_at TEXT NOT NULL
        );
        """)

init_billing_db()

def current_user(request: Request):
    uid = request.session.get("user_id")
    email = request.session.get("email")
    if not uid or not email:
        raise HTTPException(401, "Please sign in to manage billing.")
    return int(uid), str(email).strip().lower()

def flutterwave_key():
    key = os.getenv("FLW_SECRET_KEY", "").strip()
    if not key:
        raise HTTPException(503, "Flutterwave payments are not configured. The owner must add FLW_SECRET_KEY in Railway Variables.")
    return key

def flw_request(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        "https://api.flutterwave.com" + path, data=data, method=method,
        headers={"Authorization": "Bearer " + flutterwave_key(), "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise HTTPException(502, "Flutterwave rejected the request (" + str(exc.code) + "). " + detail) from exc
    except (TimeoutError, urllib.error.URLError, ValueError) as exc:
        raise HTTPException(502, "Flutterwave is temporarily unavailable. Please retry shortly.") from exc
    if body.get("status") != "success":
        raise HTTPException(502, "Flutterwave could not complete the request.")
    return body.get("data") or {}

def active_subscription(uid):
    now = datetime.now(timezone.utc)
    with db() as c:
        row = c.execute("SELECT plan,status,starts_at,expires_at,updated_at FROM billing_subscriptions WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return {"plan": "free", "status": "free", "expires_at": None, "active": True}
    plan, expiry = row["plan"], row["expires_at"]
    active = bool(plan != "free" and expiry and datetime.fromisoformat(expiry) > now)
    return {"plan": plan if active else "free", "status": "active" if active else ("expired" if plan != "free" else "free"), "expires_at": expiry, "active": active}

def apply_successful_payment(reference, transaction):
    with db() as c:
        payment = c.execute("SELECT * FROM billing_payments WHERE reference=?", (reference,)).fetchone()
        if not payment:
            return False
        if payment["status"] == "success":
            return True
        if transaction.get("status") != "successful" or transaction.get("tx_ref") != reference:
            return False
        if str(transaction.get("currency", "")).upper() != payment["currency"]:
            return False
        try:
            paid_minor = int(round(float(transaction.get("amount", 0)) * 100))
        except (TypeError, ValueError):
            return False
        if paid_minor < int(payment["amount"]):
            return False
        now = datetime.now(timezone.utc)
        existing = c.execute("SELECT expires_at FROM billing_subscriptions WHERE user_id=?", (payment["user_id"],)).fetchone()
        base = now
        if existing and existing["expires_at"]:
            try:
                previous = datetime.fromisoformat(existing["expires_at"])
                if previous > base:
                    base = previous
            except ValueError:
                pass
        expiry = (base + timedelta(days=PLANS[payment["plan"]]["period_days"])).isoformat()
        c.execute("UPDATE billing_payments SET status='success',provider_transaction_id=?,paid_at=? WHERE reference=?",
                  (str(transaction.get("id", "")), now.isoformat(), reference))
        c.execute("""INSERT INTO billing_subscriptions(user_id,plan,status,starts_at,expires_at,updated_at)
                     VALUES(?,?,'active',?,?,?)
                     ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan,status='active',
                     starts_at=excluded.starts_at,expires_at=excluded.expires_at,updated_at=excluded.updated_at""",
                  (payment["user_id"], payment["plan"], now.isoformat(), expiry, now.isoformat()))
        return True

def _verify_transaction(transaction_id):
    if not str(transaction_id).isdigit():
        raise HTTPException(422, "Invalid Flutterwave transaction ID.")
    return flw_request("/v3/transactions/" + str(transaction_id) + "/verify")

LIMITS = {"free": {"generation": 10, "audit": 3}, "creator": {"generation": 100, "audit": 25}, "pro": {"generation": 500, "audit": 100}, "agency": {"generation": 2000, "audit": 500}}

def _effective_plan(uid):
    sub = active_subscription(uid)
    return sub["plan"] if sub["active"] else "free"

def _is_admin(uid):
    admins = {x.strip().lower() for x in os.getenv("ADMIN_EMAILS", "").split(",") if x.strip()}
    with db() as c:
        row = c.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    return bool(row and row["email"].lower() in admins)

def check_usage(uid, feature):
    if feature not in ("generation", "audit") or _is_admin(uid):
        return
    plan = _effective_plan(uid)
    limit = LIMITS[plan][feature]
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with db() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM billing_usage WHERE user_id=? AND feature=? AND substr(created_at,1,7)=?", (uid, feature, month)).fetchone()
    if int(row["n"]) >= limit:
        raise HTTPException(402, f"Monthly {feature} limit reached for the {PLANS[plan]['name']} plan ({limit}). Upgrade your plan or try again next month.")

def record_usage(uid, feature):
    with db() as c:
        c.execute("INSERT INTO billing_usage(user_id,feature,created_at) VALUES(?,?,?)", (uid, feature, datetime.now(timezone.utc).isoformat()))

class CheckoutInput(BaseModel):
    plan: str = Field(min_length=1, max_length=30)

@router.get("/plans")
def list_plans():
    return {"currency": "NGN", "provider": "Flutterwave", "plans": [{"id": key, **value} for key, value in PLANS.items()]}

@router.get("/status")
def billing_status(request: Request):
    uid, email = current_user(request)
    sub = active_subscription(uid)
    admin = _is_admin(uid)
    with db() as c:
        recent = c.execute("SELECT reference,plan,amount,currency,status,created_at,paid_at FROM billing_payments WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    return {"email": email, "subscription": sub, "plans": [{"id": k, **v} for k, v in PLANS.items()], "payments": [dict(r) | {"amount": r["amount"] / 100} for r in recent], "is_admin": admin, "admin_free_access": admin}

@router.post("/checkout")
def checkout(body: CheckoutInput, request: Request):
    uid, email = current_user(request)
    plan_id = body.plan.strip().lower()
    if plan_id not in PLANS or plan_id == "free":
        raise HTTPException(422, "Choose a paid plan.")
    plan = PLANS[plan_id]
    reference = "kzsb_" + secrets.token_urlsafe(20).replace("-", "").replace("_", "")[:28]
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        c.execute("INSERT INTO billing_payments(reference,user_id,email,plan,amount,currency,status,created_at) VALUES(?,?,?,?,?,'NGN','pending',?)",
                  (reference, uid, email, plan_id, int(plan["amount"] * 100), now))
    try:
        result = flw_request("/v3/payments", "POST", {
            "tx_ref": reference, "amount": plan["amount"], "currency": "NGN",
            "redirect_url": APP_URL + "/?payment=callback",
            "customer": {"email": email, "name": email.split("@")[0]},
            "customizations": {"title": "King Zarry AI Social Booster", "description": plan["name"] + " plan"},
            "meta": {"user_id": uid, "plan": plan_id, "product": "King Zarry AI Social Booster"},
            "configuration": {"session_duration": 30, "max_retry_attempt": 3},
        })
    except Exception:
        with db() as c:
            c.execute("UPDATE billing_payments SET status='initialization_failed' WHERE reference=? AND status='pending'", (reference,))
        raise
    url = result.get("link")
    if not url or not str(url).startswith("https://checkout.flutterwave.com/"):
        raise HTTPException(502, "Flutterwave did not return a valid hosted checkout URL.")
    return {"authorization_url": url, "reference": reference, "plan": plan_id, "amount": plan["amount"], "currency": "NGN", "provider": "Flutterwave"}

@router.get("/verify")
def verify_payment(tx_ref: str, transaction_id: str, request: Request):
    uid, _ = current_user(request)
    tx_ref = tx_ref.strip()
    if not tx_ref or len(tx_ref) > 100:
        raise HTTPException(422, "Invalid payment reference.")
    with db() as c:
        payment = c.execute("SELECT * FROM billing_payments WHERE reference=? AND user_id=?", (tx_ref, uid)).fetchone()
    if not payment:
        raise HTTPException(404, "Payment reference was not found for this account.")
    if payment["status"] == "success":
        return {"ok": True, "status": "success", "subscription": active_subscription(uid)}
    transaction = _verify_transaction(transaction_id)
    if transaction.get("tx_ref") != tx_ref:
        raise HTTPException(400, "Flutterwave transaction reference did not match this checkout.")
    if not apply_successful_payment(tx_ref, transaction):
        if transaction.get("status") != "successful":
            with db() as c:
                c.execute("UPDATE billing_payments SET status=? WHERE reference=? AND status='pending'", (str(transaction.get("status", "pending"))[:40], tx_ref))
            return {"ok": False, "status": transaction.get("status", "pending"), "message": "Payment is not confirmed yet."}
        raise HTTPException(400, "Payment details did not match the expected order. Access was not activated.")
    return {"ok": True, "status": "success", "subscription": active_subscription(uid)}

@router.post("/webhook")
async def flutterwave_webhook(request: Request):
    expected = os.getenv("FLW_SECRET_HASH", "").strip()
    if not expected:
        raise HTTPException(503, "Flutterwave webhook secret hash is not configured.")
    signature = request.headers.get("verif-hash", "")
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Invalid Flutterwave webhook signature.")
    try:
        event = await request.json()
    except ValueError:
        raise HTTPException(400, "Invalid webhook body.")
    if event.get("event") == "charge.completed":
        data = event.get("data") or {}
        reference = str(data.get("tx_ref", ""))
        transaction_id = data.get("id")
        if reference and transaction_id:
            with db() as c:
                payment = c.execute("SELECT reference FROM billing_payments WHERE reference=?", (reference,)).fetchone()
            if payment:
                transaction = _verify_transaction(str(transaction_id))
                if transaction.get("tx_ref") == reference:
                    apply_successful_payment(reference, transaction)
    return {"received": True}

@router.get("/admin/sales")
def admin_sales(request: Request):
    uid, _ = current_user(request)
    if not _is_admin(uid):
        raise HTTPException(403, "Admin billing access is not enabled for this account.")
    with db() as c:
        totals = c.execute("SELECT COUNT(*) AS payment_count, COALESCE(SUM(amount),0) AS gross_minor FROM billing_payments WHERE status='success'").fetchone()
        by_plan = c.execute("SELECT plan,COUNT(*) AS sales,COALESCE(SUM(amount),0) AS gross_minor FROM billing_payments WHERE status='success' GROUP BY plan ORDER BY gross_minor DESC").fetchall()
        recent = c.execute("SELECT email,plan,amount,currency,paid_at,reference FROM billing_payments WHERE status='success' ORDER BY id DESC LIMIT 50").fetchall()
    return {"payment_count": totals["payment_count"], "gross_ngn": totals["gross_minor"] / 100, "by_plan": [{"plan": r["plan"], "sales": r["sales"], "gross_ngn": r["gross_minor"] / 100} for r in by_plan], "recent": [dict(r) | {"amount": r["amount"] / 100} for r in recent]}
