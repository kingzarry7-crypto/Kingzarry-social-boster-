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
from fastapi.responses import RedirectResponse
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            plan TEXT NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'NGN',
            status TEXT NOT NULL DEFAULT 'pending',
            provider_transaction_id TEXT,
            created_at TEXT NOT NULL,
            paid_at TEXT
        );
        CREATE TABLE IF NOT EXISTS billing_usage(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            feature TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_billing_usage_user_feature_date ON billing_usage(user_id,feature,created_at);
        CREATE TABLE IF NOT EXISTS billing_subscriptions(
            user_id INTEGER PRIMARY KEY,
            plan TEXT NOT NULL DEFAULT 'free',
            status TEXT NOT NULL DEFAULT 'free',
            starts_at TEXT,
            expires_at TEXT,
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

def paystack_key():
    key = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
    if not key:
        raise HTTPException(503, "Payments are not configured yet. The app owner must add PAYSTACK_SECRET_KEY in Railway Variables.")
    return key

def paystack_request(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        "https://api.paystack.co" + path,
        data=data,
        method=method,
        headers={"Authorization": "Bearer " + paystack_key(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise HTTPException(502, "Payment provider rejected the request (" + str(exc.code) + "). " + detail) from exc
    except (TimeoutError, urllib.error.URLError, ValueError) as exc:
        raise HTTPException(502, "Payment provider is temporarily unavailable. Try again shortly.") from exc
    if not body.get("status"):
        raise HTTPException(502, "Payment provider could not complete the request.")
    return body.get("data") or {}

def active_subscription(uid):
    now = datetime.now(timezone.utc)
    with db() as c:
        row = c.execute("SELECT plan,status,starts_at,expires_at,updated_at FROM billing_subscriptions WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return {"plan": "free", "status": "free", "expires_at": None, "active": True}
    plan = row["plan"]
    expiry = row["expires_at"]
    active = bool(plan != "free" and expiry and datetime.fromisoformat(expiry) > now)
    return {"plan": plan if active else "free", "status": "active" if active else ("expired" if plan != "free" else "free"), "expires_at": expiry, "active": active}

def apply_successful_payment(reference, transaction):
    with db() as c:
        payment = c.execute("SELECT * FROM billing_payments WHERE reference=?", (reference,)).fetchone()
        if not payment:
            return False
        if payment["status"] == "success":
            return True
        if transaction.get("status") != "success" or transaction.get("reference") != reference:
            return False
        if str(transaction.get("currency", "")).upper() != payment["currency"]:
            return False
        try:
            paid_amount = int(transaction.get("amount", 0))
        except (TypeError, ValueError):
            return False
        if paid_amount < int(payment["amount"]):
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


LIMITS = {
    "free": {"generation": 10, "audit": 3},
    "creator": {"generation": 100, "audit": 25},
    "pro": {"generation": 500, "audit": 100},
    "agency": {"generation": 2000, "audit": 500},
}

def _effective_plan(uid):
    sub = active_subscription(uid)
    return sub["plan"] if sub["active"] else "free"

def check_usage(uid, feature):
    if feature not in ("generation", "audit"):
        return
    plan = _effective_plan(uid)
    limit = LIMITS[plan][feature]
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with db() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM billing_usage WHERE user_id=? AND feature=? AND substr(created_at,1,7)=?", (uid, feature, month)).fetchone()
    if int(row["n"]) >= limit:
        plan_name = PLANS[plan]["name"]
        raise HTTPException(402, f"Monthly {feature} limit reached for the {plan_name} plan ({limit}). Upgrade your plan or try again next month.")

def record_usage(uid, feature):
    with db() as c:
        c.execute("INSERT INTO billing_usage(user_id,feature,created_at) VALUES(?,?,?)", (uid, feature, datetime.now(timezone.utc).isoformat()))

class CheckoutInput(BaseModel):
    plan: str = Field(min_length=1, max_length=30)

@router.get("/plans")
def list_plans():
    return {"currency": "NGN", "plans": [{"id": key, **value} for key, value in PLANS.items()]}

@router.get("/status")
def billing_status(request: Request):
    uid, email = current_user(request)
    sub = active_subscription(uid)
    with db() as c:
        recent = c.execute("SELECT reference,plan,amount,currency,status,created_at,paid_at FROM billing_payments WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    admins = {x.strip().lower() for x in os.getenv("ADMIN_EMAILS", "").split(",") if x.strip()}
    return {"email": email, "subscription": sub, "plans": [{"id": k, **v} for k, v in PLANS.items()], "payments": [dict(r) for r in recent], "is_admin": email in admins}

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
        result = paystack_request("/transaction/initialize", "POST", {
            "email": email,
            "amount": str(int(plan["amount"] * 100)),
            "currency": "NGN",
            "reference": reference,
            "callback_url": APP_URL + "/?payment=callback",
            "metadata": {"user_id": uid, "plan": plan_id, "product": "King Zarry AI Social Booster"},
        })
    except Exception:
        with db() as c:
            c.execute("UPDATE billing_payments SET status='initialization_failed' WHERE reference=? AND status='pending'", (reference,))
        raise
    url = result.get("authorization_url")
    if not url or not str(url).startswith("https://checkout.paystack.com/"):
        raise HTTPException(502, "Payment provider did not return a valid checkout link.")
    return {"authorization_url": url, "reference": reference, "plan": plan_id, "amount": plan["amount"], "currency": "NGN"}

@router.get("/verify")
def verify_payment(reference: str, request: Request):
    uid, _ = current_user(request)
    reference = reference.strip()
    if not reference or len(reference) > 100:
        raise HTTPException(422, "Invalid payment reference.")
    with db() as c:
        payment = c.execute("SELECT * FROM billing_payments WHERE reference=? AND user_id=?", (reference, uid)).fetchone()
    if not payment:
        raise HTTPException(404, "Payment reference was not found for this account.")
    if payment["status"] == "success":
        return {"ok": True, "status": "success", "subscription": active_subscription(uid)}
    transaction = paystack_request("/transaction/verify/" + reference)
    if not apply_successful_payment(reference, transaction):
        if transaction.get("status") not in ("success",):
            with db() as c:
                c.execute("UPDATE billing_payments SET status=? WHERE reference=? AND status='pending'", (str(transaction.get("status", "pending"))[:40], reference))
            return {"ok": False, "status": transaction.get("status", "pending"), "message": "Payment is not confirmed yet."}
        raise HTTPException(400, "Payment details did not match the expected order. Access was not activated.")
    return {"ok": True, "status": "success", "subscription": active_subscription(uid)}

@router.post("/webhook")
async def paystack_webhook(request: Request):
    secret = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
    if not secret:
        raise HTTPException(503, "Payment webhook is not configured.")
    raw = await request.body()
    signature = request.headers.get("x-paystack-signature", "")
    expected = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Invalid webhook signature.")
    try:
        event = json.loads(raw.decode())
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "Invalid webhook body.")
    if event.get("event") == "charge.success":
        data = event.get("data") or {}
        reference = str(data.get("reference", ""))
        if reference:
            with db() as c:
                payment = c.execute("SELECT reference FROM billing_payments WHERE reference=?", (reference,)).fetchone()
            if payment:
                verified = paystack_request("/transaction/verify/" + reference)
                apply_successful_payment(reference, verified)
    return {"received": True}

@router.get("/admin/sales")
def admin_sales(request: Request):
    _, email = current_user(request)
    admins = {x.strip().lower() for x in os.getenv("ADMIN_EMAILS", "").split(",") if x.strip()}
    if not admins or email not in admins:
        raise HTTPException(403, "Admin billing access is not enabled for this account.")
    with db() as c:
        totals = c.execute("SELECT COUNT(*) AS payment_count, COALESCE(SUM(amount),0) AS gross_kobo FROM billing_payments WHERE status='success'").fetchone()
        by_plan = c.execute("SELECT plan,COUNT(*) AS sales,COALESCE(SUM(amount),0) AS gross_kobo FROM billing_payments WHERE status='success' GROUP BY plan ORDER BY gross_kobo DESC").fetchall()
        recent = c.execute("SELECT email,plan,amount,currency,paid_at,reference FROM billing_payments WHERE status='success' ORDER BY id DESC LIMIT 50").fetchall()
    return {"payment_count": totals["payment_count"], "gross_ngn": totals["gross_kobo"] / 100, "by_plan": [{"plan": r["plan"], "sales": r["sales"], "gross_ngn": r["gross_kobo"] / 100} for r in by_plan], "recent": [dict(r) | {"amount": r["amount"] / 100} for r in recent]}
