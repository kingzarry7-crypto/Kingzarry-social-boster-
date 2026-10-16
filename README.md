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

The billing UI uses Flutterwave Standard checkout in NGN. Add these variables to Railway → `social-booster-api` → Variables. Do not put secrets in GitHub or frontend code.

- `FLW_SECRET_KEY`: Flutterwave test secret key for initial testing; switch to live only after merchant activation and test verification.
- `FLW_SECRET_HASH`: webhook secret hash configured in Flutterwave Dashboard → Settings → Webhooks.
- `ADMIN_EMAILS`: comma-separated email address(es) used to sign in as the app owner. These accounts receive admin sales access and unlimited AI generation/profile-audit quotas.
- `COOKIE_SECURE=true` and `APP_SECRET`: keep secure session cookies and a stable random session/encryption secret.

Configure this webhook URL in Flutterwave:

`https://social-booster-api-production.up.railway.app/api/billing/webhook`

The backend verifies each transaction with Flutterwave before activating access and verifies webhook signatures. Payment records and plan access are stored in the persistent database. Current tiers are Free (10 AI generations and 3 profile audits/month), Creator (₦3,000/30 days), Pro (₦8,000/30 days), and Agency (₦25,000/30 days). Paid access is currently a 30-day purchase with manual renewal; recurring billing is not enabled. Gross sales are not net bank payouts: provider fees, refunds and settlement timing may affect the final payout. Railway hosts the application but does not receive the money; Flutterwave settles it to the bank account configured in your merchant account.

## Email and SMS signup verification

New registrations require two separate one-time codes: one sent by email and one by SMS. Accounts are not created until both codes are correct. Codes expire after 10 minutes and attempts are limited. Real delivery is fail-closed: if provider credentials are absent or delivery fails, the app reports an error instead of pretending a code was sent.

Configure these variables on Railway:

- `SMTP_HOST`: SMTP server hostname from your email provider.
- `SMTP_PORT`: usually `587` for STARTTLS, or `465` when `SMTP_USE_SSL=true`.
- `SMTP_USER`: SMTP login username.
- `SMTP_PASSWORD`: SMTP password or provider app password.
- `SMTP_FROM_EMAIL`: verified sender email address.
- `SMTP_USE_SSL`: optional; `true` for SSL SMTP, otherwise STARTTLS is used.
- `TERMII_API_KEY`: Termii API key.
- `TERMII_SENDER_ID`: sender ID approved by Termii for your account.
- `PHONE_COUNTRY_CODE`: optional default country code for local numbers; defaults to `+234`.

Before public signup, test that both the email and SMS arrive, and confirm incorrect/expired codes cannot create accounts. Email and SMS delivery fees/limits depend on the providers. Never log or return OTP codes.

## User accounts and social account connections

Every customer signs in with their own Social Booster account. Each OAuth connection is saved against that logged-in user's ID, so users authorize their own Facebook/Instagram, TikTok, YouTube or X account through the provider's official consent page; the app must never collect social account passwords. The owner does not connect all customers' accounts centrally.

Add the provider credentials listed in the Social OAuth table above to Railway. Then register each exact callback URL with that platform, enable the required products/scopes, and complete any platform review/approval. Without real provider credentials and approvals, social connection buttons should show a configuration error; they cannot complete real authorization. Supported publishing differs by platform; YouTube video upload and automatic scheduled publishing are not implemented.
