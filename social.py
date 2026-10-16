import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/social", tags=["social accounts"])
DB_PATH = os.getenv("DATABASE_PATH", str(Path(__file__).resolve().parent / "social_booster.db"))
APP_URL = os.getenv("APP_URL", "https://social-booster-api-production.up.railway.app").rstrip("/")
OAUTH_PLATFORMS = ("instagram", "facebook", "tiktok", "youtube", "x")

def _now():
    return datetime.now(timezone.utc).isoformat()

def _fernet():
    secret = os.getenv("APP_SECRET")
    if not secret:
        raise HTTPException(503, "APP_SECRET must be configured before storing social tokens.")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)

def _seal(value):
    return _fernet().encrypt(value.encode()).decode()

def _open(value):
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise HTTPException(503, "Stored social token cannot be decrypted. Check that APP_SECRET has not changed.") from exc

def _db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn

def init_social_db():
    with _db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS social_accounts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            provider_user_id TEXT NOT NULL,
            handle TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            token_cipher TEXT NOT NULL,
            refresh_cipher TEXT,
            token_expires_at INTEGER,
            scopes TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, platform, provider_user_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS social_oauth_audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,platform TEXT NOT NULL,
            action TEXT NOT NULL,created_at TEXT NOT NULL
        )""")

init_social_db()

def _uid(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Please sign in to manage social accounts.")
    return int(uid)

def _required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(503, "Social integration is not configured: set " + name + " in Railway.")
    return value

def _config(platform):
    configs = {
        "facebook": {"client_id":"META_APP_ID","client_secret":"META_APP_SECRET","authorize":"https://www.facebook.com/v23.0/dialog/oauth","token":"https://graph.facebook.com/v23.0/oauth/access_token","scopes":["pages_show_list","pages_read_engagement","pages_manage_posts","instagram_basic","instagram_content_publish"]},
        "instagram": {"client_id":"META_APP_ID","client_secret":"META_APP_SECRET","authorize":"https://www.facebook.com/v23.0/dialog/oauth","token":"https://graph.facebook.com/v23.0/oauth/access_token","scopes":["pages_show_list","pages_read_engagement","pages_manage_posts","instagram_basic","instagram_content_publish"]},
        "tiktok": {"client_id":"TIKTOK_CLIENT_KEY","client_secret":"TIKTOK_CLIENT_SECRET","authorize":"https://www.tiktok.com/v2/auth/authorize/","token":"https://open.tiktokapis.com/v2/oauth/token/","scopes":["user.info.basic","user.info.profile","user.info.stats","video.publish","video.upload"]},
        "youtube": {"client_id":"GOOGLE_CLIENT_ID","client_secret":"GOOGLE_CLIENT_SECRET","authorize":"https://accounts.google.com/o/oauth2/v2/auth","token":"https://oauth2.googleapis.com/token","scopes":["openid","email","profile","https://www.googleapis.com/auth/youtube.readonly","https://www.googleapis.com/auth/youtube.upload"]},
        "x": {"client_id":"X_CLIENT_ID","client_secret":"X_CLIENT_SECRET","authorize":"https://twitter.com/i/oauth2/authorize","token":"https://api.x.com/2/oauth2/token","scopes":["tweet.read","tweet.write","users.read","offline.access"]},
    }
    if platform not in configs:
        raise HTTPException(404, "Unsupported social platform.")
    cfg = dict(configs[platform])
    cfg["client_id"] = _required_env(cfg["client_id"])
    cfg["client_secret"] = _required_env(cfg["client_secret"])
    cfg["redirect_uri"] = APP_URL + "/api/social/callback/" + platform
    return cfg

def _http_json(url, method="GET", payload=None, headers=None, form=False):
    data = None
    h = dict(headers or {})
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode()
            h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:700]
        except Exception:
            pass
        raise HTTPException(502, "Social provider returned HTTP " + str(exc.code) + ": " + detail) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise HTTPException(502, "Could not reach the social provider. Check the integration and try again.") from exc

def _store(uid, platform, provider_id, handle, display_name, token, refresh, expires_in, scopes, metadata):
    expires_at = int(time.time()) + int(expires_in) if expires_in else None
    with _db() as c:
        c.execute("""INSERT INTO social_accounts(user_id,platform,provider_user_id,handle,display_name,token_cipher,refresh_cipher,token_expires_at,scopes,metadata,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id,platform,provider_user_id) DO UPDATE SET handle=excluded.handle,display_name=excluded.display_name,token_cipher=excluded.token_cipher,refresh_cipher=excluded.refresh_cipher,token_expires_at=excluded.token_expires_at,scopes=excluded.scopes,metadata=excluded.metadata,updated_at=excluded.updated_at""",
        (uid,platform,str(provider_id),handle or "",display_name or "",_seal(token),_seal(refresh) if refresh else None,expires_at," ".join(scopes or []),json.dumps(metadata or {}),_now(),_now()))

def _access_token(row):
    token = _open(row["token_cipher"])
    expires_at = row["token_expires_at"]
    refresh_cipher = row["refresh_cipher"]
    if not expires_at or expires_at > int(time.time()) + 90 or not refresh_cipher:
        return token
    platform = row["platform"]
    cfg = _config(platform)
    refresh = _open(refresh_cipher)
    payload = {"grant_type":"refresh_token","refresh_token":refresh}
    headers = {}
    if platform == "youtube":
        payload.update({"client_id":cfg["client_id"],"client_secret":cfg["client_secret"]})
    elif platform == "tiktok":
        payload.update({"client_key":cfg["client_id"],"client_secret":cfg["client_secret"]})
    elif platform == "x":
        payload["client_id"] = cfg["client_id"]
        headers["Authorization"] = "Basic " + base64.b64encode((cfg["client_id"]+":"+cfg["client_secret"]).encode()).decode()
    else:
        # Meta page tokens may remain valid longer than the user token; reconnect if the provider rejects them.
        return token
    data = _http_json(cfg["token"],"POST",payload,headers,form=True)
    new_token = data.get("access_token")
    if not new_token:
        return token
    new_refresh = data.get("refresh_token") or refresh
    new_expiry = int(time.time()) + int(data.get("expires_in",3600))
    with _db() as c:
        c.execute("UPDATE social_accounts SET token_cipher=?,refresh_cipher=?,token_expires_at=?,updated_at=? WHERE id=?",
                  (_seal(new_token),_seal(new_refresh),new_expiry,_now(),row["id"]))
    return new_token

def _account(row):
    return {"id":row["id"],"platform":row["platform"],"provider_user_id":row["provider_user_id"],"username":row["handle"],"display_name":row["display_name"],"connected":True,"scopes":row["scopes"].split(),"expires_at":row["token_expires_at"],"metadata":json.loads(row["metadata"] or "{}"),"connected_at":row["created_at"]}

@router.get("/accounts")
def list_accounts(request: Request):
    uid = _uid(request)
    with _db() as c:
        rows = c.execute("SELECT * FROM social_accounts WHERE user_id=? ORDER BY platform,handle",(uid,)).fetchall()
    return {"accounts":[_account(x) for x in rows],"configured":{p:bool(os.getenv(_config_env(p))) for p in OAUTH_PLATFORMS}}

def _config_env(platform):
    return {"instagram":"META_APP_ID","facebook":"META_APP_ID","tiktok":"TIKTOK_CLIENT_KEY","youtube":"GOOGLE_CLIENT_ID","x":"X_CLIENT_ID"}[platform]

@router.get("/connect/{platform}")
def connect(platform: str, request: Request):
    uid = _uid(request)
    platform = platform.lower()
    cfg = _config(platform)
    state = secrets.token_urlsafe(32)
    request.session["social_oauth_state"] = {"state":state,"platform":platform,"uid":uid,"created":int(time.time())}
    query = {"client_id":cfg["client_id"],"redirect_uri":cfg["redirect_uri"],"response_type":"code","scope":" ".join(cfg["scopes"]),"state":state}
    if platform in ("youtube",):
        query.update({"access_type":"offline","prompt":"consent","include_granted_scopes":"true"})
    if platform == "tiktok":
        query["client_key"] = query.pop("client_id")
    if platform == "x":
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        request.session["social_oauth_state"]["verifier"] = verifier
        query.update({"code_challenge":challenge,"code_challenge_method":"S256"})
    return RedirectResponse(cfg["authorize"] + "?" + urllib.parse.urlencode(query), status_code=302)

@router.get("/callback/{platform}")
def callback(platform: str, request: Request, code: str = "", state: str = "", error: str = ""):
    platform = platform.lower()
    saved = request.session.get("social_oauth_state") or {}
    request.session.pop("social_oauth_state", None)
    if error:
        raise HTTPException(400, "Social authorization was declined: " + error[:120])
    if not code or not state or not secrets.compare_digest(str(saved.get("state","")),state) or saved.get("platform") != platform or int(saved.get("created",0)) < int(time.time())-600:
        raise HTTPException(400, "OAuth state validation failed or expired. Start the connection again.")
    uid = _uid(request)
    cfg = _config(platform)
    payload = {"client_id":cfg["client_id"],"client_secret":cfg["client_secret"],"code":code,"redirect_uri":cfg["redirect_uri"],"grant_type":"authorization_code"}
    headers = {}
    if platform == "tiktok":
        payload = {"client_key":cfg["client_id"],"client_secret":cfg["client_secret"],"code":code,"redirect_uri":cfg["redirect_uri"],"grant_type":"authorization_code"}
        token_data = _http_json(cfg["token"],"POST",payload,form=True)
        token_data = token_data.get("data",token_data)
    elif platform == "x":
        payload["code_verifier"] = saved.get("verifier","")
        basic = base64.b64encode((cfg["client_id"]+":"+cfg["client_secret"]).encode()).decode()
        headers["Authorization"] = "Basic " + basic
        token_data = _http_json(cfg["token"],"POST",payload,headers=headers,form=True)
    elif platform == "youtube":
        token_data = _http_json(cfg["token"],"POST",payload,form=True)
    else:
        payload["redirect_uri"] = cfg["redirect_uri"]
        token_data = _http_json(cfg["token"]+"?"+urllib.parse.urlencode(payload))
    token = token_data.get("access_token")
    if not token:
        raise HTTPException(502,"The provider did not return an access token.")
    refresh = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")
    scopes = token_data.get("scope", "")
    if isinstance(scopes,str):
        scopes = scopes.replace(","," ").split()
    profile = {}
    if platform in ("facebook","instagram"):
        profile_data = _http_json("https://graph.facebook.com/v23.0/me?fields=id,name&access_token="+urllib.parse.quote(token))
        pages_data = _http_json("https://graph.facebook.com/v23.0/me/accounts?fields=id,name,username,access_token,instagram_business_account{id,username}&access_token="+urllib.parse.quote(token))
        pages = pages_data.get("data",[])
        saved_any = False
        for page in pages:
            page_id = str(page.get("id",""))
            if not page_id: continue
            page_token = page.get("access_token") or token
            _store(uid,"facebook",page_id,page.get("username") or page.get("name",""),page.get("name",""),page_token,refresh,expires_in,scopes,{"page_id":page_id,"page_name":page.get("name","")})
            saved_any = True
            ig = page.get("instagram_business_account")
            if ig and ig.get("id"):
                _store(uid,"instagram",ig["id"],ig.get("username",""),ig.get("username",""),page_token,refresh,expires_in,scopes,{"instagram_user_id":str(ig["id"]),"facebook_page_id":page_id,"page_name":page.get("name","")})
                saved_any = True
        if not saved_any:
            _store(uid,"facebook",profile_data.get("id","me"),profile_data.get("name",""),profile_data.get("name",""),token,refresh,expires_in,scopes,{"note":"No managed pages returned; publishing may require a Facebook Page."})
        profile = profile_data
    elif platform == "tiktok":
        data = _http_json("https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name,username,avatar_url",headers={"Authorization":"Bearer "+token})
        profile = data.get("data",{}).get("user",{})
        _store(uid,platform,profile.get("open_id","unknown"),profile.get("username",""),profile.get("display_name",""),token,refresh,expires_in,scopes,profile)
    elif platform == "youtube":
        profile = _http_json("https://www.googleapis.com/oauth2/v2/userinfo",headers={"Authorization":"Bearer "+token})
        channel = _http_json("https://www.googleapis.com/youtube/v3/channels?part=id,snippet&mine=true",headers={"Authorization":"Bearer "+token})
        item = (channel.get("items") or [{}])[0]
        _store(uid,platform,item.get("id") or profile.get("id","unknown"),profile.get("email",""),item.get("snippet",{}).get("title") or profile.get("name",""),token,refresh,expires_in,scopes,{"channel":item,"email":profile.get("email","")})
    else:
        profile_data = _http_json("https://api.x.com/2/users/me?user.fields=public_metrics",headers={"Authorization":"Bearer "+token})
        profile = profile_data.get("data",{})
        _store(uid,platform,profile.get("id","unknown"),profile.get("username",""),profile.get("name",""),token,refresh,expires_in,scopes,profile)
    with _db() as c:
        c.execute("INSERT INTO social_oauth_audit(user_id,platform,action,created_at) VALUES(?,?,?,?)",(uid,platform,"connected",_now()))
    return RedirectResponse(APP_URL + "/?social_connected=" + urllib.parse.quote(platform),status_code=303)

@router.delete("/accounts/{account_id}")
def disconnect(account_id: int, request: Request):
    uid = _uid(request)
    with _db() as c:
        cur = c.execute("DELETE FROM social_accounts WHERE id=? AND user_id=?",(account_id,uid))
        if cur.rowcount:
            c.execute("INSERT INTO social_oauth_audit(user_id,platform,action,created_at) VALUES(?,?,?,?)",(uid,"unknown","disconnected",_now()))
    if not cur.rowcount:
        raise HTTPException(404,"Connected account not found.")
    return {"ok":True,"disconnected":True}

class PublishInput(BaseModel):
    account_id: int
    text: str = Field(default="", max_length=5000)
    media_url: str = ""
    title: str = Field(default="", max_length=200)
    privacy_level: str = "PUBLIC_TO_EVERYONE"

def _get_account(uid, account_id):
    with _db() as c:
        row = c.execute("SELECT * FROM social_accounts WHERE id=? AND user_id=?",(account_id,uid)).fetchone()
    if not row:
        raise HTTPException(404,"Connected account not found.")
    return row

@router.post("/publish")
def publish(body: PublishInput, request: Request):
    uid = _uid(request)
    row = _get_account(uid,body.account_id)
    token = _open(row["token_cipher"])
    platform = row["platform"]
    meta = json.loads(row["metadata"] or "{}")
    text = body.text.strip()
    if platform == "facebook":
        if not text:
            raise HTTPException(422,"Post text is required.")
        result = _http_json("https://graph.facebook.com/v23.0/"+urllib.parse.quote(row["provider_user_id"])+"/feed","POST",{"message":text,"access_token":token},form=True)
        return {"ok":True,"platform":platform,"published":True,"post_id":result.get("id"),"result":result}
    if platform == "instagram":
        if not body.media_url.startswith("https://"):
            raise HTTPException(422,"Instagram publishing requires a publicly reachable HTTPS image URL.")
        container = _http_json("https://graph.facebook.com/v23.0/"+urllib.parse.quote(row["provider_user_id"])+"/media","POST",{"image_url":body.media_url,"caption":text,"access_token":token},form=True)
        result = _http_json("https://graph.facebook.com/v23.0/"+urllib.parse.quote(row["provider_user_id"])+"/media_publish","POST",{"creation_id":container.get("id"),"access_token":token},form=True)
        return {"ok":True,"platform":platform,"published":True,"post_id":result.get("id"),"result":result}
    if platform == "x":
        if not text:
            raise HTTPException(422,"Post text is required.")
        result = _http_json("https://api.x.com/2/tweets","POST",{"text":text},{"Authorization":"Bearer "+token})
        return {"ok":True,"platform":platform,"published":True,"post_id":result.get("data",{}).get("id"),"result":result}
    if platform == "tiktok":
        if not body.media_url.startswith("https://"):
            raise HTTPException(422,"TikTok direct posting requires a publicly reachable HTTPS video URL.")
        creator = _http_json("https://open.tiktokapis.com/v2/post/publish/creator_info/query/", "POST", {}, {"Authorization":"Bearer "+token})
        options = creator.get("data",{}).get("privacy_level_options",[])
        privacy = body.privacy_level if body.privacy_level in options else (options[0] if options else "")
        if not privacy:
            raise HTTPException(502,"TikTok did not return allowed privacy options.")
        result = _http_json("https://open.tiktokapis.com/v2/post/publish/video/init/","POST",{"post_info":{"title":(body.title or text)[:150],"privacy_level":privacy,"disable_duet":False,"disable_stitch":False,"disable_comment":False},"source_info":{"source":"PULL_FROM_URL","video_url":body.media_url}},{"Authorization":"Bearer "+token})
        return {"ok":True,"platform":platform,"published":False,"status":"submitted","publish_id":result.get("data",{}).get("publish_id"),"result":result,"note":"TikTok accepted the submission request; poll publish status before reporting it published."}
    if platform == "youtube":
        raise HTTPException(501,"YouTube OAuth and channel access are connected, but video upload requires a video-file upload flow. Use the saved calendar until that upload flow is enabled.")
    raise HTTPException(400,"Publishing is not supported for this account.")

@router.get("/analytics/{platform}")
def analytics(platform: str, request: Request):
    uid = _uid(request)
    platform = platform.lower()
    with _db() as c:
        rows = c.execute("SELECT * FROM social_accounts WHERE user_id=? AND platform=? ORDER BY id",(uid,platform)).fetchall()
    if not rows:
        raise HTTPException(404,"No connected account for this platform.")
    results = []
    for row in rows:
        token = _open(row["token_cipher"])
        metrics = {}
        if platform == "youtube":
            data = _http_json("https://www.googleapis.com/youtube/v3/channels?part=statistics,snippet&mine=true",headers={"Authorization":"Bearer "+token})
            item = (data.get("items") or [{}])[0]
            metrics = {"title":item.get("snippet",{}).get("title"),"statistics":item.get("statistics",{})}
        elif platform in ("facebook","instagram"):
            fields = "id,name,fan_count,followers_count,engagement,media_count"
            data = _http_json("https://graph.facebook.com/v23.0/"+urllib.parse.quote(row["provider_user_id"])+"?fields="+urllib.parse.quote(fields)+"&access_token="+urllib.parse.quote(token))
            metrics = data
        elif platform == "x":
            data = _http_json("https://api.x.com/2/users/me?user.fields=public_metrics",headers={"Authorization":"Bearer "+token})
            metrics = data.get("data",{})
        elif platform == "tiktok":
            data = _http_json("https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name,username,follower_count,following_count,likes_count,video_count",headers={"Authorization":"Bearer "+token})
            metrics = data.get("data",{}).get("user",{})
        results.append({"account":_account(row),"metrics":metrics,"retrieved_at":_now()})
    return {"platform":platform,"accounts":results,"source":"official_api"}

@router.get("/status")
def social_status(request: Request):
    uid = _uid(request)
    with _db() as c:
        rows = c.execute("SELECT platform,COUNT(*) n FROM social_accounts WHERE user_id=? GROUP BY platform",(uid,)).fetchall()
    connected = {x["platform"]:x["n"] for x in rows}
    configured = {p:bool(os.getenv(_config_env(p)) and os.getenv({"facebook":"META_APP_SECRET","instagram":"META_APP_SECRET","tiktok":"TIKTOK_CLIENT_SECRET","youtube":"GOOGLE_CLIENT_SECRET","x":"X_CLIENT_SECRET"}[p])) for p in OAUTH_PLATFORMS}
    return {"platforms":[{"platform":p,"configured":configured[p],"connected_accounts":connected.get(p,0),"connected":connected.get(p,0)>0} for p in OAUTH_PLATFORMS]}
