import hashlib
import hmac
import json
import os
import secrets
import smtplib
import sqlite3
import urllib.parse
import urllib.request
from email.message import EmailMessage
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "King Zarry AI Social Booster"
BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "social_booster.db"))
APP_SECRET = os.getenv("APP_SECRET") or secrets.token_urlsafe(48)
PLATFORMS = ["Instagram", "TikTok", "YouTube", "Facebook", "X"]
CONTENT_TYPES = {"caption", "hashtags", "video_script", "content_ideas", "calendar", "campaign_plan", "profile_audit"}

app = FastAPI(title=APP_NAME, version="1.0.0", description="Ethical AI social growth workspace")
app.add_middleware(SessionMiddleware, secret_key=APP_SECRET, same_site="lax", https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true")

from social import router as social_router
from billing import router as billing_router, check_usage, record_usage
app.include_router(social_router)
app.include_router(billing_router)

@contextmanager
def db():
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL,created_at TEXT NOT NULL,phone TEXT NOT NULL DEFAULT '',email_verified INTEGER NOT NULL DEFAULT 1,phone_verified INTEGER NOT NULL DEFAULT 1);\n        CREATE TABLE IF NOT EXISTS pending_signups(email TEXT PRIMARY KEY,phone TEXT NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL,email_code_hash TEXT NOT NULL,sms_code_hash TEXT NOT NULL,expires_at TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS campaigns(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,name TEXT NOT NULL,platform TEXT NOT NULL,goal TEXT NOT NULL,audience TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'draft',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS content_items(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,platform TEXT NOT NULL,kind TEXT NOT NULL,topic TEXT NOT NULL,body TEXT NOT NULL,scheduled_at TEXT,status TEXT NOT NULL DEFAULT 'draft',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audits(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,platform TEXT NOT NULL,username TEXT NOT NULL,niche TEXT NOT NULL,result TEXT NOT NULL,created_at TEXT NOT NULL);
        """)

init_db()

def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()
    return salt.hex(), digest

def verify_password(password, salt_hex, expected):
    _, actual = password_hash(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(actual, expected)

def user_id(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Please sign in to continue.")
    return int(uid)

def required(value, label="This field", limit=2000):
    value = " ".join(str(value or "").split()).strip()
    if not value:
        raise HTTPException(422, label + " is required.")
    return value[:limit]

def platform_name(value):
    result = next((p for p in PLATFORMS if p.lower() == str(value or "").lower()), None)
    if not result:
        raise HTTPException(422, "Platform must be one of: " + ", ".join(PLATFORMS))
    return result

class AuthInput(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=128)

class RegisterInput(AuthInput):
    phone: str = Field(min_length=7, max_length=30)

class VerifySignupInput(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    email_code: str = Field(min_length=6, max_length=6)
    sms_code: str = Field(min_length=6, max_length=6)

class GenerateInput(BaseModel):
    platform: str
    kind: str
    topic: str
    audience: str = ""
    tone: str = "helpful"
    goal: str = "grow an authentic audience"

class AuditInput(BaseModel):
    platform: str
    username: str
    niche: str
    bio: str = ""
    recent_posts: list[str] = Field(default_factory=list, max_length=10)
    goals: str = "increase reach and meaningful engagement"

class CampaignInput(BaseModel):
    name: str
    platform: str
    goal: str
    audience: str = ""

class ContentInput(BaseModel):
    platform: str
    kind: str
    topic: str
    body: str
    scheduled_at: str | None = None

def ai_complete(system_prompt, user_prompt, max_tokens=1200):
    key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("AI_MODEL", "").strip()
    base = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    if not key or not model:
        return None
    payload = json.dumps({"model": model, "messages": [{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}], "temperature":0.7,"max_tokens":max_tokens}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=payload, method="POST", headers={"Authorization":"Bearer " + key,"Content-Type":"application/json","HTTP-Referer":os.getenv("APP_URL","http://localhost:8080"),"X-Title":APP_NAME})
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            return json.loads(response.read().decode())["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as exc:
        raise HTTPException(502, "AI provider returned HTTP " + str(exc.code) + ". Check API key, model, quota and base URL.") from exc
    except (TimeoutError, urllib.error.URLError, ValueError, KeyError, IndexError) as exc:
        raise HTTPException(502, "AI provider unavailable or returned an invalid response. Try again or check server configuration.") from exc

def fallback_content(kind, platform, topic, audience, tone):
    notes = {
        "Instagram":"Lead with a strong first line, keep paragraphs short, and use a clear save/share prompt.",
        "TikTok":"Open with a 1–2 second hook, speak naturally, and finish with one clear call to action.",
        "YouTube":"Make the promise specific, deliver useful steps, and invite a relevant comment.",
        "Facebook":"Use a conversational opening, practical detail, and a question that invites genuine discussion.",
        "X":"Make one clear point, keep it concise, and invite thoughtful replies rather than engagement bait."
    }
    if kind == "hashtags":
        return "Suggested starting tags for " + platform + " about " + topic + ":\n#" + "".join(topic.title().split()) + " #CreatorTips #LearnTogether #CommunityFirst\n\nUse only accurate, relevant tags; avoid unrelated trending tags."
    if kind == "video_script":
        return "VIDEO SCRIPT — " + platform + "\n\nHOOK (0–3s): “Here is one thing about " + topic + " I wish I knew earlier…”\n\nVALUE:\n1. Explain a common mistake.\n2. Show a practical example.\n3. Give one action viewers can try today.\n\nCLOSE: “What would you like me to explain next?”\n\n" + notes[platform]
    if kind == "content_ideas":
        return "5 content ideas for " + platform + " about " + topic + ":\n1. Beginner mistake and how to fix it.\n2. Step-by-step tutorial.\n3. Myth versus fact.\n4. Behind-the-scenes process.\n5. Answer a real audience question.\n\n" + notes[platform]
    if kind == "calendar":
        return "7-DAY CONTENT PLAN — " + platform + " / " + topic + "\nDay 1: Beginner tip.\nDay 2: Tutorial.\nDay 3: Behind-the-scenes story.\nDay 4: Answer a frequent question.\nDay 5: Myth versus fact.\nDay 6: Community question.\nDay 7: Weekly recap.\n\nBatch content and review real results weekly."
    if kind == "campaign_plan":
        return "CAMPAIGN: " + topic + "\nGoal: " + (audience or "reach the right audience") + "\n1. Pick one audience and one measurable outcome.\n2. Publish useful teaching, demonstration and story formats.\n3. Keep your bio and next step clear.\n4. Respond to genuine comments.\n5. Review reach, watch time, saves, shares and clicks where available.\n6. Repeat what works; never buy followers or spam."
    return topic + "\n\n" + notes[platform] + "\n\nIf this was useful, save it and share your experience.\nTone: " + tone + ". Audience: " + (audience or "people interested in this topic") + "."

@app.get("/health")
def health():
    return {"status":"ok","service":"king-zarry-social-booster","time":now_iso()}

@app.get("/api/status")
def status():
    return {"name":APP_NAME,"platforms":PLATFORMS,"ai_configured":bool(os.getenv("OPENAI_API_KEY") and os.getenv("AI_MODEL")),"ai_provider":os.getenv("OPENAI_BASE_URL","https://openrouter.ai/api/v1"),"features":{"profile_audit":True,"content_studio":True,"campaigns":True,"content_calendar":True,"official_social_oauth":True,"publishing":{"facebook":True,"instagram":True,"tiktok":"submission flow; provider status must be polled","x":True,"youtube":False},"live_analytics":{"facebook":True,"instagram":True,"tiktok":True,"youtube":True,"x":True}},"note":"OAuth endpoints and supported publishing/analytics adapters are implemented. Provider credentials, app review, account permissions and user authorization are required before real actions work."}

def normalize_phone(value):
    phone = "".join(ch for ch in str(value or "").strip() if ch.isdigit() or ch == "+")
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if phone.startswith("0") and len(phone) == 11:
        phone = os.getenv("PHONE_COUNTRY_CODE", "+234").strip() + phone[1:]
    if not phone.startswith("+") or not phone[1:].isdigit() or not (8 <= len(phone[1:]) <= 15):
        raise HTTPException(422, "Enter a valid international phone number, e.g. +2348012345678.")
    return phone

def otp_digest(email, channel, code):
    return hmac.new(APP_SECRET.encode(), (email + "|" + channel + "|" + code).encode(), hashlib.sha256).hexdigest()

def send_email_otp(email, code):
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM_EMAIL", username).strip()
    if not all((host, username, password, sender)):
        raise HTTPException(503, "Email verification is not configured. Add SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_FROM_EMAIL in Railway Variables.")
    message = EmailMessage()
    message["Subject"] = "Your King Zarry Social Booster verification code"
    message["From"] = sender
    message["To"] = email
    message.set_content("Your King Zarry Social Booster email verification code is: " + code + "\n\nThis code expires in 10 minutes. If you did not request it, ignore this email.")
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        if os.getenv("SMTP_USE_SSL", "false").lower() == "true":
            with smtplib.SMTP_SSL(host, port, timeout=20) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(username, password)
                server.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        raise HTTPException(502, "The email provider could not send your verification code. Check SMTP settings and try again.") from exc

def send_sms_otp(phone, code):
    api_key = os.getenv("TERMII_API_KEY", "").strip()
    sender = os.getenv("TERMII_SENDER_ID", "").strip()
    if not api_key or not sender:
        raise HTTPException(503, "SMS verification is not configured. Add TERMII_API_KEY and an approved TERMII_SENDER_ID in Railway Variables.")
    payload = json.dumps({
        "api_key": api_key, "to": phone, "from": sender,
        "sms": "Your King Zarry Social Booster verification code is " + code + ". It expires in 10 minutes.",
        "type": "plain", "channel": "generic"
    }).encode()
    req = urllib.request.Request("https://api.ng.termii.com/api/sms/send", data=payload, method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode())
        if str(result.get("code", "")) not in ("ok", "200", "0") and result.get("message", "").lower() not in ("message sent", "ok"):
            raise ValueError("SMS provider did not confirm acceptance")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(502, "The SMS provider could not send your verification code. Check Termii balance, sender ID approval and settings.") from exc

@app.post("/api/auth/register")
def register(body: RegisterInput):
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(422, "Enter a valid email address.")
    phone = normalize_phone(body.phone)
    if not (os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD") and os.getenv("TERMII_API_KEY") and os.getenv("TERMII_SENDER_ID")):
        raise HTTPException(503, "Signup verification is not ready. The owner must configure SMTP email and Termii SMS credentials in Railway Variables first. No account has been created.")
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "An account with this email already exists. Sign in instead.")
        existing = c.execute("SELECT 1 FROM pending_signups WHERE email=?", (email,)).fetchone()
    salt, digest = password_hash(body.password)
    email_code = f"{secrets.randbelow(1000000):06d}"
    sms_code = f"{secrets.randbelow(1000000):06d}"
    expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    with db() as c:
        c.execute("""INSERT INTO pending_signups(email,phone,salt,password_hash,email_code_hash,sms_code_hash,expires_at,attempts,created_at)
                     VALUES(?,?,?,?,?,?,?,0,?)
                     ON CONFLICT(email) DO UPDATE SET phone=excluded.phone,salt=excluded.salt,password_hash=excluded.password_hash,
                     email_code_hash=excluded.email_code_hash,sms_code_hash=excluded.sms_code_hash,expires_at=excluded.expires_at,attempts=0,created_at=excluded.created_at""",
                  (email, phone, salt, digest, otp_digest(email, "email", email_code), otp_digest(email, "sms", sms_code), expires, now_iso()))
    # Fail honestly if either real provider cannot deliver. Codes are never returned to the browser.
    send_email_otp(email, email_code)
    send_sms_otp(phone, sms_code)
    return {"ok": True, "verification_required": True, "email": email, "phone_masked": phone[:4] + "••••" + phone[-3:], "expires_in_seconds": 600, "message": "Verification codes sent. Enter the email code and SMS code to finish creating your account."}

@app.post("/api/auth/verify-signup")
def verify_signup(body: VerifySignupInput, request: Request):
    email = body.email.strip().lower()
    with db() as c:
        pending = c.execute("SELECT * FROM pending_signups WHERE email=?", (email,)).fetchone()
    if not pending:
        raise HTTPException(404, "No pending signup was found. Start account creation again.")
    if datetime.fromisoformat(pending["expires_at"]) <= datetime.now(timezone.utc):
        with db() as c:
            c.execute("DELETE FROM pending_signups WHERE email=?", (email,))
        raise HTTPException(410, "Verification codes expired. Start account creation again.")
    if int(pending["attempts"]) >= 5:
        with db() as c:
            c.execute("DELETE FROM pending_signups WHERE email=?", (email,))
        raise HTTPException(429, "Too many incorrect codes. Start account creation again.")
    valid_email = hmac.compare_digest(pending["email_code_hash"], otp_digest(email, "email", body.email_code.strip()))
    valid_sms = hmac.compare_digest(pending["sms_code_hash"], otp_digest(email, "sms", body.sms_code.strip()))
    if not (valid_email and valid_sms):
        with db() as c:
            c.execute("UPDATE pending_signups SET attempts=attempts+1 WHERE email=?", (email,))
        raise HTTPException(400, "Email code or SMS code is incorrect.")
    try:
        with db() as c:
            cur = c.execute("INSERT INTO users(email,salt,password_hash,created_at,phone,email_verified,phone_verified) VALUES(?,?,?,?,?,1,1)",
                            (email, pending["salt"], pending["password_hash"], now_iso(), pending["phone"]))
            uid = cur.lastrowid
            c.execute("DELETE FROM pending_signups WHERE email=?", (email,))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account with this email already exists. Sign in instead.")
    request.session.clear()
    request.session["user_id"] = int(uid)
    request.session["email"] = email
    admins = {x.strip().lower() for x in os.getenv("ADMIN_EMAILS", "").split(",") if x.strip()}
    return {"ok": True, "email": email, "role": "admin" if email in admins else "user", "plan": "free", "admin_free_access": email in admins}

@app.post("/api/auth/login")
def login(body: AuthInput, request: Request):
    email = body.email.strip().lower()
    with db() as c:
        row = c.execute("SELECT id,email,salt,password_hash FROM users WHERE email=?",(email,)).fetchone()
    if not row or not verify_password(body.password,row["salt"],row["password_hash"]):
        raise HTTPException(401,"Email or password is incorrect.")
    request.session.clear()
    request.session["user_id"] = int(row["id"])
    request.session["email"] = row["email"]
    admins = {x.strip().lower() for x in os.getenv("ADMIN_EMAILS", "").split(",") if x.strip()}\n    return {"ok":True,"email":row["email"],"role":"admin" if row["email"].lower() in admins else "user","admin_free_access":row["email"].lower() in admins}

@app.post("/api/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok":True}

@app.get("/api/me")
def me(request: Request):
    email = str(request.session.get("email", "")).lower()\n    admins = {x.strip().lower() for x in os.getenv("ADMIN_EMAILS", "").split(",") if x.strip()}\n    return {"email":email,"user_id":user_id(request),"role":"admin" if email in admins else "user","plan":"free","admin_free_access":email in admins}

@app.get("/api/dashboard")
def dashboard(request: Request):
    uid = user_id(request)
    with db() as c:
        counts = {
            "campaigns":c.execute("SELECT COUNT(*) n FROM campaigns WHERE user_id=?",(uid,)).fetchone()["n"],
            "drafts":c.execute("SELECT COUNT(*) n FROM content_items WHERE user_id=? AND status='draft'",(uid,)).fetchone()["n"],
            "audits":c.execute("SELECT COUNT(*) n FROM audits WHERE user_id=?",(uid,)).fetchone()["n"]
        }
        content = c.execute("SELECT id,platform,kind,topic,body,status,scheduled_at,created_at FROM content_items WHERE user_id=? ORDER BY id DESC LIMIT 6",(uid,)).fetchall()
        campaigns = c.execute("SELECT id,name,platform,goal,status,created_at FROM campaigns WHERE user_id=? ORDER BY id DESC LIMIT 5",(uid,)).fetchall()
    return {"stats":counts,"recent_content":[dict(x) for x in content],"campaigns":[dict(x) for x in campaigns]}

@app.post("/api/ai/generate")
def generate(body: GenerateInput, request: Request):
    uid = user_id(request)
    check_usage(uid, "generation")
    platform = platform_name(body.platform)
    kind = body.kind.strip().lower()
    if kind not in CONTENT_TYPES - {"profile_audit"}:
        raise HTTPException(422,"Unsupported content type.")
    topic = required(body.topic,"Topic",500)
    audience = (body.audience or "").strip()[:500]
    tone = required(body.tone,"Tone",80)
    prompt = "Create " + kind + " for " + platform + ". Topic: " + topic + ". Audience: " + (audience or "not specified") + ". Tone: " + tone + ". Goal: " + body.goal[:300] + ". Be useful and specific. Do not invent metrics or recommend fake followers, fake likes, spam DMs, or policy evasion."
    result = ai_complete("You are King Zarry AI Social Booster, an expert ethical social media strategist. Help creators grow through valuable content and authentic community engagement. Never claim an account is connected or content published unless verified by an integration.",prompt)
    source = "ai-provider" if result else "built-in"
    record_usage(uid, "generation")
    return {"content":result or fallback_content(kind,platform,topic,audience,tone),"source":source,"platform":platform,"kind":kind}

@app.post("/api/audit")
def audit(body: AuditInput, request: Request):
    uid = user_id(request)
    check_usage(uid, "audit")
    platform = platform_name(body.platform)
    username = required(body.username.lstrip("@"),"Username",100).replace(" ","")
    niche = required(body.niche,"Niche",300)
    bio = (body.bio or "").strip()[:1000]
    posts = [str(x).strip()[:300] for x in body.recent_posts[:10] if str(x).strip()]
    prompt = "Create an actionable profile audit for " + platform + " username @" + username + ". Niche: " + niche + ". User-supplied bio: " + (bio or "(not supplied)") + ". User-supplied post descriptions: " + json.dumps(posts) + ". Goal: " + body.goals[:300] + ". Include positioning, bio improvements, content gaps, 5 next-post ideas, 2-week experiment and metrics to track. Do not invent followers, engagement rates, current posts or live analytics. State uncertainty."
    result = ai_complete("You are a careful ethical social media consultant. Never claim you accessed the account or scraped data.",prompt,1500)
    if not result:
        result = "PROFILE AUDIT — @" + username + " (" + platform + ")\n\nScope: Starter audit based only on details you supplied; this is not a live account scan.\n\nPOSITIONING\n- Explain who you help, what outcome you offer, and what to do next.\n- Make the promise specific to " + niche + ".\n\nBIO CHECKLIST\n- Lead with audience and value.\n- Add only genuine proof.\n- Use one clear call to action.\n\nCONTENT TO TEST\n1. Beginner how-to.\n2. Common mistake and correction.\n3. Behind-the-scenes process.\n4. Case study with verified results.\n5. Answer a frequent question.\n\n2-WEEK EXPERIMENT\n- Publish 3–5 useful pieces weekly at a sustainable cadence.\n- Test one hook or format at a time.\n- Reply to genuine comments.\n- Track reach, watch time, saves, shares, profile visits and clicks where available.\n\nAdd your bio and recent post descriptions for a more tailored review. No live metrics were retrieved."
    record_usage(uid, "audit")
    with db() as c:
        cur = c.execute("INSERT INTO audits(user_id,platform,username,niche,result,created_at) VALUES(?,?,?,?,?,?)",(uid,platform,username,niche,result,now_iso()))
        audit_id = cur.lastrowid
    return {"id":audit_id,"platform":platform,"username":username,"result":result,"source":"ai-provider" if os.getenv("OPENAI_API_KEY") and os.getenv("AI_MODEL") else "built-in"}

@app.get("/api/audits")
def list_audits(request: Request):
    uid = user_id(request)
    with db() as c:
        rows = c.execute("SELECT id,platform,username,niche,result,created_at FROM audits WHERE user_id=? ORDER BY id DESC LIMIT 30",(uid,)).fetchall()
    return {"items":[dict(x) for x in rows]}

@app.post("/api/campaigns")
def create_campaign(body: CampaignInput, request: Request):
    uid = user_id(request)
    name = required(body.name,"Campaign name",160)
    platform = platform_name(body.platform)
    goal = required(body.goal,"Campaign goal",500)
    audience = (body.audience or "").strip()[:500]
    with db() as c:
        cur = c.execute("INSERT INTO campaigns(user_id,name,platform,goal,audience,status,created_at) VALUES(?,?,?,?,?,'draft',?)",(uid,name,platform,goal,audience,now_iso()))
        cid = cur.lastrowid
    return {"id":cid,"name":name,"platform":platform,"goal":goal,"audience":audience,"status":"draft"}

@app.get("/api/campaigns")
def list_campaigns(request: Request):
    uid = user_id(request)
    with db() as c:
        rows = c.execute("SELECT id,name,platform,goal,audience,status,created_at FROM campaigns WHERE user_id=? ORDER BY id DESC",(uid,)).fetchall()
    return {"items":[dict(x) for x in rows]}

@app.post("/api/content")
def save_content(body: ContentInput, request: Request):
    uid = user_id(request)
    platform = platform_name(body.platform)
    kind = body.kind.strip().lower()
    if kind not in CONTENT_TYPES:
        raise HTTPException(422,"Unsupported content type.")
    topic = required(body.topic,"Topic",500)
    content = required(body.body,"Content",12000)
    scheduled = (body.scheduled_at or "").strip() or None
    with db() as c:
        cur = c.execute("INSERT INTO content_items(user_id,platform,kind,topic,body,scheduled_at,status,created_at) VALUES(?,?,?,?,?,?,?,?)",(uid,platform,kind,topic,content,scheduled,"scheduled" if scheduled else "draft",now_iso()))
        iid = cur.lastrowid
    return {"id":iid,"platform":platform,"kind":kind,"topic":topic,"body":content,"scheduled_at":scheduled,"status":"scheduled" if scheduled else "draft"}

@app.get("/api/content")
def list_content(request: Request):
    uid = user_id(request)
    with db() as c:
        rows = c.execute("SELECT id,platform,kind,topic,body,scheduled_at,status,created_at FROM content_items WHERE user_id=? ORDER BY COALESCE(scheduled_at,created_at) DESC LIMIT 100",(uid,)).fetchall()
    return {"items":[dict(x) for x in rows]}

@app.delete("/api/content/{item_id}")
def delete_content(item_id: int, request: Request):
    uid = user_id(request)
    with db() as c:
        cur = c.execute("DELETE FROM content_items WHERE id=? AND user_id=?",(item_id,uid))
    if not cur.rowcount:
        raise HTTPException(404,"Content item not found.")
    return {"ok":True}

@app.get("/")
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")

@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code,content={"detail":exc.detail,"status":exc.status_code})
