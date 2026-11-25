# Browser login

The web client signs in through `POST /auth/login` and restores the account through
`GET /me` on page load. The HTTP-only `agenthub_session` cookie lasts **24 hours
from sign-in**; page reloads do not extend that deadline. Closing and reopening
the browser retains login if it retains cookies. Private browsing or clearing site
data may end the session earlier. JavaScript never stores passwords or session
tokens in localStorage.

Migration `0010_browser_sessions` adds an `auth_sessions` table containing only a
token digest, user ID, password fingerprint, creation time, and absolute expiry.
All API replicas share this state. Every authenticated request checks expiry and
account status. Password changes and account deactivation invalidate sessions.
Login rotates the current browser's token and clears expired records without
revoking other devices. Logout revokes the server record and expires the cookie;
the client reports failures instead of pretending logout succeeded. Open SSE
streams recheck sessions before polling for further events.

Cookies use HttpOnly, SameSite=Lax, and Path=/. HTTPS sets Secure, and the cloud API
explicitly sets `SESSION_COOKIE_SECURE=true` because Caddy terminates TLS. Local
Compose supports loopback HTTP. An HTTP-only Swarm rehearsal can explicitly set
`SESSION_COOKIE_SECURE=false`; public HTTPS deployments must keep it true.

Login, logout, and cookie-authenticated writes require `X-AgentHub-Request: 1`.
Cross-site forms cannot supply this non-simple header; the API does not enable
credentialed cross-origin CORS. The client also sends `X-AgentHub-Account` so a
stale tab cannot silently act as another account after a different tab signs in.
401 errors omit the Basic challenge so the browser does not open a second prompt.
Explicit HTTP Basic API clients remain supported. Invalid explicit credentials
never fall back to another account's cookie. Ownership, approvals, queue ordering,
and submission idempotency are unchanged.

Run the additive migration before updating APIs. Older images ignore the new
table. Image rollback may require signing in again through the old client; do
not downgrade the database during an application-only rollback.
