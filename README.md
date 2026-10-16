# King Zarry AI Social Booster

A deployable FastAPI + SQLite social growth workspace for ethical, real-audience growth.

## Features
- Dashboard with platform overview, campaign planning, content calendar, and reports.
- Username/profile audit using user-supplied profile details (does not scrape private data or bypass platform restrictions).
- AI content studio for platform-specific captions, hashtags, hooks, scripts, and content calendars.
- Campaigns and scheduled-post records stored in SQLite.
- Provider-ready AI integration using an OpenAI-compatible endpoint (OpenRouter recommended).
- Health endpoint and Railway deployment support.
- Social account connection/publishing actions are intentionally disabled until official platform OAuth credentials and approved API scopes are configured. No fake followers, likes, spam, password collection, or unauthorized automation.

## Run locally
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```
Open http://localhost:8000. API docs: http://localhost:8000/docs.

## Railway deployment
1. Create a Railway project from this repository.
2. Railway detects the Dockerfile automatically.
3. Set `APP_SECRET` to a long random secret.
4. Optional AI settings: `OPENAI_BASE_URL=https://openrouter.ai/api/v1`, `OPENAI_API_KEY`, and `AI_MODEL` (e.g. a model available in your account).
5. Set `DATABASE_PATH=/data/social_booster.db` and attach a Railway Volume mounted at `/data` to preserve SQLite data between deploys.
6. Deploy. Check `/health`.

## Configuration
- `APP_SECRET`: signs session cookies. Required for production.
- `OPENAI_API_KEY`: optional API key for content generation.
- `OPENAI_BASE_URL`: OpenAI-compatible API base URL.
- `AI_MODEL`: model identifier supported by the configured provider.
- `DATABASE_PATH`: SQLite file path.
- `ALLOWED_ORIGINS`: comma-separated frontend origins if hosting UI separately; same-origin works by default.

## Important limitations
This initial production-oriented MVP does not claim to connect social accounts until OAuth integrations are implemented and configured. Public profile analysis accepts information provided by the user and may use the AI provider to generate suggestions. Metrics are not fabricated: live analytics require each platform's official API, granted scopes, and app approval. Never place provider keys in browser code.
