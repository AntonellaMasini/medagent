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

### Reference: Cigna's discovered endpoint chain (May 2026)

This is the chain the Cigna scraper should use post-login. Documented here so the next person doesn't have to rediscover it:

```
Playwright login (NIE + password [+ SMS OTP])
   │ (session cookies now set across *.cigna.es)
   ▼
GET /cp/api/private/chipcard?insuranceNumber=<NIE>01
        → cardNumber (chipcard, stable per user)
        → policy.code (groupCode, stable per user)
        → subscriberNumber (echoes the NIE)

GET /dm/api/tuotempo/session-id-token?chipcard=<chipcard>&authorized=true
        → rotating session token (Java signed int, can be negative)

GET /dm/api/policies/<session-token>
        → idMember (stable per user)

GET /dm/api/providers/advanced-search?
        chipcard=<chipcard>&idMember=<idMember>&groupCode=<groupCode>&
        specialtyMedicalActDescription=<Specialty.name>&
        type=<Specialty.type>&
        limit=20&offset=0&orderType=DEFAULT&directionSort=DESC&
        publishable=true&outpatient=N
        → list of doctors with name, clinic, address, phone, geoPoint, distance
```

Notes:
- The `01` suffix on `<NIE>01` is the person number within the policy. `01` is the primary policyholder; family members would be `02`/`03`/.... MVP scope assumes primary-only.
- The advanced-search response includes `geoPoint.lat/lon` and `distanceToSearchPoint`, so the Cigna adapter does **not** need `infrastructure/external/google_maps.py` for distance/geocoding. Other insurers may still need it.
- The `Specialty` value object (`domain/value_objects/specialty.py`) carries both `name` and `type` — pass them directly to the search params as `specialtyMedicalActDescription` and `type`.

---

## (Reserved for future conventions)

Add new project-wide rules here as they emerge. Keep entries small and scoped — anything that's just "how a single module works" belongs in code comments or docstrings, not this file.
