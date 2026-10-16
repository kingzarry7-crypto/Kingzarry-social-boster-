# King Zarry AI Social Booster

FastAPI + responsive dashboard for ethical social media growth.

## Railway service

- Health: `/health`
- API docs: `/docs`
- Persistent SQLite: `/data/social_booster.db` (mount volume at `/data`)
- Service URL: https://social-booster-api-production.up.railway.app/

## Required environment

Set these on the Booster service in Railway; do not commit secrets:

- `APP_SECRET`: long random secret; keep stable to preserve sessions and decrypt saved tokens.
- `COOKIE_SECURE=true`
- `DATABASE_PATH=/data/social_booster.db`
- `APP_URL=https://social-booster-api-production.up.railway.app`
- `OPENAI_BASE_URL=https://api.openai.com/v1` (or a compatible provider API base)
- `OPENAI_API_KEY`: provider secret
- `AI_MODEL=gpt-4o-mini` (or a model available to your provider)

Provider secrets are not automatically copied from another Railway project. Add the key using Railway's variable editor.

## Social OAuth credentials

Register each OAuth app with these exact HTTPS redirect URIs and add the variables to the Booster service.

| Platform | Variables | Redirect URI |
| --- | --- | --- |
| Meta (Facebook + Instagram) | `META_APP_ID`, `META_APP_SECRET` | `https://social-booster-api-production.up.railway.app/api/social/callback/facebook` and `https://social-booster-api-production.up.railway.app/api/social/callback/instagram` |
| TikTok | `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` | `https://social-booster-api-production.up.railway.app/api/social/callback/tiktok` |
| YouTube / Google | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | `https://social-booster-api-production.up.railway.app/api/social/callback/youtube` |
| X | `X_CLIENT_ID`, `X_CLIENT_SECRET` | `https://social-booster-api-production.up.railway.app/api/social/callback/x` |

Provider approvals, valid client credentials, approved scopes and account eligibility are required. The app reports a connection only after a successful callback. Tokens are encrypted at rest using a key derived from `APP_SECRET`; changing that secret requires users to reconnect unless tokens are migrated.

## Social routes

- `GET /api/social/status` — configuration and connection state.
- `GET /api/social/accounts` — connected accounts; tokens are never returned.
- `GET /api/social/connect/{platform}` and `GET /api/social/callback/{platform}` — OAuth authorization.
- `DELETE /api/social/accounts/{account_id}` — disconnect an account.
- `POST /api/social/publish` — Facebook Page text, Instagram image URL + caption, X text, and TikTok video-URL submission. TikTok submission is not confirmed publication; poll provider status. YouTube video upload is not implemented.
- `GET /api/social/analytics/{platform}` — requests official API metrics if permissions and account type allow.

The app never asks for social passwords. The content calendar currently stores plans; a background scheduler for automatic publishing is not enabled. Profile audits use supplied profile details and do not claim to scrape live public metrics.
