# Project conventions for Claude Code

This file is auto-loaded into every Claude Code conversation in this repo. Keep it focused on **decisions and rules that apply across the codebase** — not implementation details (the code is the source of truth for those).

Personal preferences and conversational notes live in the off-repo memory at `~/.claude/projects/...` instead; this file is for shared knowledge anyone on the project benefits from.

---

## Scraper architecture: browser for auth, API for actions

When building an insurer adapter (`infrastructure/scrapers/<insurer>_scraper.py`):

- **Playwright is used only for the authentication flow** — typing credentials, OTP screens, captchas, persisting cookies via `storage_state`.
- **All subsequent data fetches go through the insurer's internal JSON API** via `context.request.get(...)` calls on the authenticated browser context. The cookies set during the Playwright login flow automatically.
- **Don't default to HTML scraping** even though Playwright makes it easy. API calls are ~5× faster, ~100× lighter on memory, and far more stable than CSS selectors against a redesigning frontend.

### Process for a new insurer adapter

Before writing any code:

1. Log into the insurer's portal with **DevTools Network tab open**.
2. Perform every action the agent needs: search by specialty, view appointments, book, cancel.
3. Map the API endpoints. Note the auth scheme (cookies, headers, query params), the response shapes, and any user-scoped values you need to grab post-login.
4. **Only fall back to HTML scraping if no API exists for that specific action** — or as a temporary workaround when an API breaks.

This 30-minute discovery pass usually saves days of selector tuning.

### When Playwright UI scraping IS the right choice

- Login + OTP flows (the auth itself is a browser flow)
- Captchas, anti-bot challenges
- Actions for which no API endpoint is exposed
- API broke and you need a temporary workaround while we re-discover the new shape

### Practical defaults for `context.request.get(...)`

- **Always set `Accept: application/json`** explicitly. Some insurer endpoints (e.g. Cigna's `directorio-medico`) content-negotiate to XML when no `Accept` is set, which silently breaks JSON parsing.
- **Don't persist rotating session tokens.** Re-fetch on every login. They're cheap and avoid stale-token debugging six months out.
- **Do persist stable per-user identifiers** (e.g. Cigna's `chipcard`, `groupCode`, `idMember`) — see the user-row encryption pattern in `infrastructure/persistence/sqlite_user_repository.py` for how.

### Reference: Cigna's discovered endpoint chain (last verified 2026-05-23)

This is the chain the Cigna scraper should use post-login. Cigna's portal is an Okta-fronted SPA — `login.clientes.cigna.es/idp/idx/...` does auth, then `clientes.cigna.es/cp/api/...` and `directorio-medico.cigna.es/dm/api/...` do data. The chain has drifted since the original mapping; if you see API errors after a Cigna deploy, **re-verify by opening the portal in DevTools and watching the Network tab** — that's the only source of truth for an internal API.

```
Playwright login (NIE/NIF/Pasaporte + password + SMS OTP via Okta)
   │ (session cookies set across *.cigna.es)
   ▼
GET /cp/api/private/home
        → insuranceNumber (the user's REAL insurance number, e.g. "Z3512875K01")

   ⚠️ The login identifier (what the user types) is NOT the insuranceNumber.
   Cigna accepts NIE/NIF/Pasaporte for login. Internal APIs always want the
   policy's NIE+person-suffix from /home. For users who logged in with a
   passport, the two values are completely different.

GET /cp/api/private/chipcard?insuranceNumber=<from /home>
        → cardNumber (chipcard, stable per user)
        → policy.code (groupCode, stable per user)
        → subscriberNumber

GET /dm/api/tuotempo/session-id-token?chipcard=<chipcard>&authorized=true
        → ots_token (rotating session token; short alphanumeric like "zoor92dpptafn")

   The token field used to be `sessionId` / `session-id-token` / a bare int.
   The current API returns `{"executionTime":..., "message":..., "result":"OK", "ots_token":...}`.
   `fetch_session_token()` tries multiple keys in priority order to tolerate drift.

idMember: read it from a JWT cookie, NOT from /dm/api/policies/<token>.

   The original chain used GET /dm/api/policies/<session-token> → idMember.
   That endpoint now returns 400 + HTML. The same value lives as the
   `memberid` claim in the JWT that Cigna's SPA sets as a cookie right
   after login (cookie name varies, value always starts with "eyJ").
   `fetch_id_member()` scans cookies for the JWT and decodes the payload.

GET /dm/api/providers/advanced-search?
        chipcard=<chipcard>&idMember=<memberid>&groupCode=<groupCode>&
        specialtyMedicalActDescription=<Specialty.name>&
        type=<Specialty.type>&
        limit=20&offset=0&orderType=DEFAULT&directionSort=DESC&
        publishable=true&outpatient=N
        → list of doctors with name, clinic, address, phone, geoPoint, distance
```

Notes:
- The `01` suffix on `Z3512875K01` is the person number within the policy. `01` is the primary policyholder; family members would be `02`/`03`/.... MVP scope assumes primary-only.
- The advanced-search response includes `geoPoint.lat/lon` and `distanceToSearchPoint`, so the Cigna adapter does **not** need `infrastructure/external/google_maps.py` for distance/geocoding. Other insurers may still need it.
- The `Specialty` value object (`domain/value_objects/specialty.py`) carries both `name` and `type` — pass them directly to the search params as `specialtyMedicalActDescription` and `type`.

### Cigna anti-bot hardening (Okta + OneTrust)

Cigna's portal applies several anti-bot patterns that broke a naive Playwright integration. Captured here so the next adapter doesn't waste a day re-discovering them.

- **`navigator.webdriver` fingerprint**: must be patched to `false` via `context.add_init_script` before any page script runs. Cost-bearing endpoints (SMS dispatch) silently drop requests with this flag set — the HTTP returns 200 but no SMS is sent.
- **OneTrust cookie banner**: Cigna's OneTrust instance re-injects DOM nodes after dismiss attempts and intercepts pointer events for the whole page. The reliable fix is to hide the OneTrust SDK containers (`#onetrust-consent-sdk` et al.) via CSS injected through `add_init_script`. The banner stays in the DOM but can't grab clicks.
- **Synthetic clicks**: Playwright's `click(force=True)` and JS `element.click()` both dispatch events with `isTrusted=false`. For the cost-bearing buttons (Acceder, Enviar Código, Verificar), this fails server-side validation. Use Playwright's native `.click()` (it goes through CDP and produces `isTrusted=true`).
- **OTP session binding**: Okta's OTP is bound to the session that requested it. A fresh browser invalidates the code. The scraper takes a `wait_for_otp` callback (see `OTPProvider` in `application/ports.py`) and blocks inline at the OTP screen, keeping the browser open across the user's WhatsApp reply. Do NOT re-launch the browser between requesting the OTP and submitting it.
- **SPA-driven post-auth navigation**: after OTP submit, Cigna's Angular SPA writes the access token to localStorage and navigates to `/cp/cuadro-medico` itself. A `page.goto()` here would force a reload that drops the in-flight tokens. Wait for the SPA's natural URL change with `wait_for_url(lambda u: "/cp/login" not in u)`.
- **Locale**: pass `locale="es-ES"` + `Accept-Language: es-ES` to `new_context`. Without these the page renders in English and the text-based selectors (`"Verifícate"`, `"Acceder"`, `"Aceptar todas"`) miss.

---

## (Reserved for future conventions)

Add new project-wide rules here as they emerge. Keep entries small and scoped — anything that's just "how a single module works" belongs in code comments or docstrings, not this file.
