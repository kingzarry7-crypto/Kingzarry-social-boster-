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

## Payments and owner earnings

The billing UI uses Paystack checkout in NGN. Add the following in Railway → `social-booster-api` → Variables:

- `PAYSTACK_SECRET_KEY`: your Paystack secret key. Use a test secret first; switch to a live secret only after testing and activating your Paystack merchant account. Never commit this key to GitHub or place it in frontend code.
- `ADMIN_EMAILS`: comma-separated email address(es) that should see the owner sales summary, for example the email you use to sign in as the app owner.

Set the Paystack webhook URL to:

`https://social-booster-api-production.up.railway.app/api/billing/webhook`

The webhook checks Paystack's signature and re-verifies successful transactions before granting access. The browser return also verifies the transaction server-side. Payment records and plan access are stored in the persistent database. Current tiers are Free (10 AI generations and 3 profile audits/month), Creator (₦3,000/30 days), Pro (₦8,000/30 days), and Agency (₦25,000/30 days). Paid tiers include monthly quotas shown in the app. Paid access is currently a 30-day purchase with manual renewal; automatic recurring charges are not enabled. The admin gross-sales view is not the same as net bank payout: provider fees, refunds and settlement timing may affect what reaches your bank.

Railway hosts the application; it does not receive customer payments. Customers pay through Paystack, and settlement goes to the bank account configured in your Paystack merchant account. Confirm test-mode payments first, then use live credentials for real customer payments.
