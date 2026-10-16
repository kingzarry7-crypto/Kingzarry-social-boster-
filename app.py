import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
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
app.include_router(social_router)

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
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL,created_at TEXT NOT NULL);
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

@app.post("/api/auth/register")
def register(body: AuthInput, request: Request):
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(422, "Enter a valid email address.")
    salt, digest = password_hash(body.password)
    try:
        with db() as c:
            cur = c.execute("INSERT INTO users(email,salt,password_hash,created_at) VALUES(?,?,?,?)",(email,salt,digest,now_iso()))
            uid = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account with this email already exists. Sign in instead.")
    request.session.clear()
    request.session["user_id"] = int(uid)
    request.session["email"] = email
    return {"ok":True,"email":email}

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
    return {"ok":True,"email":row["email"]}

@app.post("/api/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok":True}

@app.get("/api/me")
def me(request: Request):
    return {"email":request.session.get("email",""),"user_id":user_id(request)}

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
    user_id(request)
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
    return {"content":result or fallback_content(kind,platform,topic,audience,tone),"source":source,"platform":platform,"kind":kind}

@app.post("/api/audit")
def audit(body: AuditInput, request: Request):
    uid = user_id(request)
    platform = platform_name(body.platform)
    username = required(body.username.lstrip("@"),"Username",100).replace(" ","")
    niche = required(body.niche,"Niche",300)
    bio = (body.bio or "").strip()[:1000]
    posts = [str(x).strip()[:300] for x in body.recent_posts[:10] if str(x).strip()]
    prompt = "Create an actionable profile audit for " + platform + " username @" + username + ". Niche: " + niche + ". User-supplied bio: " + (bio or "(not supplied)") + ". User-supplied post descriptions: " + json.dumps(posts) + ". Goal: " + body.goals[:300] + ". Include positioning, bio improvements, content gaps, 5 next-post ideas, 2-week experiment and metrics to track. Do not invent followers, engagement rates, current posts or live analytics. State uncertainty."
    result = ai_complete("You are a careful ethical social media consultant. Never claim you accessed the account or scraped data.",prompt,1500)
    if not result:
        result = "PROFILE AUDIT — @" + username + " (" + platform + ")\n\nScope: Starter audit based only on details you supplied; this is not a live account scan.\n\nPOSITIONING\n- Explain who you help, what outcome you offer, and what to do next.\n- Make the promise specific to " + niche + ".\n\nBIO CHECKLIST\n- Lead with audience and value.\n- Add only genuine proof.\n- Use one clear call to action.\n\nCONTENT TO TEST\n1. Beginner how-to.\n2. Common mistake and correction.\n3. Behind-the-scenes process.\n4. Case study with verified results.\n5. Answer a frequent question.\n\n2-WEEK EXPERIMENT\n- Publish 3–5 useful pieces weekly at a sustainable cadence.\n- Test one hook or format at a time.\n- Reply to genuine comments.\n- Track reach, watch time, saves, shares, profile visits and clicks where available.\n\nAdd your bio and recent post descriptions for a more tailored review. No live metrics were retrieved."
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
