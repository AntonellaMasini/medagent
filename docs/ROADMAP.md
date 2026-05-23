# MedAgent — Roadmap

> The 10,000-ft view. For individual work items, see [GitHub issues](https://github.com/AntonellaMasini/medagent/issues). For the original vision, see [`medagent_project_brief.md`](../medagent_project_brief.md). For engineering conventions, see [`CLAUDE.md`](../CLAUDE.md).

**Last updated:** 2026-05-23

## Vision (one paragraph)

WhatsApp-driven AI agent that books private health insurance appointments in Spain. User texts *"necesito un psicólogo"*; the agent logs into the user's insurer's portal, finds nearby doctors, calls clinics with AI voice until one confirms a slot that fits the user's calendar, and replies with the booking. First insurer adapter: **Cigna Spain**. Built open-source so the community can add more insurers (Adeslas, Sanitas, …).

## Where we are right now

**Phase 1 (Foundation) — ✅ done**, **Phase 2 (Cigna live) — ✅ done** (this PR), **Phase 3 (Voice + Calendar) — 🚧 next**, beyond — not started.

### Phase 1: Foundation — DONE

| Capability | Notes |
|---|---|
| DDD scaffold (domain / application / infrastructure / interfaces) | strict layer rules; CI enforces lint + tests + migrations |
| WhatsApp webhook + Twilio inbound/outbound | verified live against a real phone |
| Onboarding state machine over WhatsApp + token-gated credentials form | name → address → insurer → preferred time → magic link → NIE/password |
| User persistence (SQLite + Fernet-encrypted credentials) | survives restarts |
| Specialty catalog from real Cigna data (194 entries, Spanish-only search) | accent-tolerant + gender-suffix-tolerant matching |
| Alembic migrations + drift check in CI | no model-vs-DB drift can land |

### Phase 2: Cigna live — DONE

`CignaScraper` no longer parses HTML. After Playwright handles login + OTP, all data fetches go through Cigna's authenticated JSON API via `CignaApiClient` — `chipcard → session-id-token → policies → advanced-search`. Geo coordinates + distances come from the API, so `google_maps.py` is no longer a hard dep for Cigna. The `Doctor` entity now models a (practitioner × clinic) tuple with proper Cigna identifiers (`clinic_id`, `practitioner_id`).

| Status | Issue | What |
|---|---|---|
| ✅ done | [#18](https://github.com/AntonellaMasini/medagent/issues/18) | Switch Cigna scraper from HTML parsing to authenticated `/dm/api/providers/advanced-search` chain |

### Phase 3: Voice + Calendar — NEXT

Now that we can reliably get doctors from Cigna, the remaining MVP pieces are the voice caller and the calendar integration. Voice is the gnarly one.

| Status | Issue | What |
|---|---|---|
| 🚧 next | [#3](https://github.com/AntonellaMasini/medagent/issues/3) | Wire slot validation into voice caller — small (~half day) |
| 📋 pending | [#4](https://github.com/AntonellaMasini/medagent/issues/4) | Real Twilio Voice + ElevenLabs/LLM voice caller — the hard one |
| 📋 pending | [#5](https://github.com/AntonellaMasini/medagent/issues/5) | Google Calendar integration (makes travel buffer real) |

**End of Phase 3 = functional MVP**: user texts "necesito un psicólogo", agent searches Cigna, calls clinics with AI voice, confirms a slot that fits the calendar, replies with the booking. End-to-end against a real phone.

### Beyond MVP

| Status | Issue | What |
|---|---|---|
| 📋 enhancement | [#15](https://github.com/AntonellaMasini/medagent/issues/15) | LLM intent parsing fallback for free-text specialty queries |
| 📋 enhancement | [#7](https://github.com/AntonellaMasini/medagent/issues/7) | Favorites system for repeat doctors |
| 📋 community | [#8](https://github.com/AntonellaMasini/medagent/issues/8) / [#11](https://github.com/AntonellaMasini/medagent/issues/11) | Adeslas insurer adapter (open PR [#10](https://github.com/AntonellaMasini/medagent/pull/10) from external contributor) |
| 📋 hardening | [#19](https://github.com/AntonellaMasini/medagent/issues/19) | Durability + retry-safety (MessageSid idempotency, outbox, etc.) — needed before scaling beyond a few users |
| 📋 dev-quality | [#20](https://github.com/AntonellaMasini/medagent/issues/20) | CI check that `docs/schema.dbml` stays in sync with models |

## Architectural decisions made

These are durable choices that shape future work. Worth knowing before contributing:

| Decision | Where documented |
|---|---|
| **DDD + Hexagonal + Clean Architecture** layering, strict inward-only dependencies | code structure speaks for itself; see `domain/`, `application/`, `infrastructure/`, `interfaces/` |
| **"Browser for auth, API for actions"** — Playwright only handles login/OTP, everything else uses the insurer's JSON API via `context.request` | `CLAUDE.md` |
| **`name` is the canonical key for `Specialty`**, not `catalog_id` — Cigna leaves ~32 entries with empty catalog_id | `domain/value_objects/specialty.py` + PR #16 |
| **Specialty snapshot on appointment row** survives Cigna renames (same pattern as `doctor_name`) | `infrastructure/persistence/database.py::AppointmentRow.doctor_specialty_name` |
| **Onboarding `OnboardingDraft` is a separate table from `users`** — credentials never live in the draft; arrive via HTTPS form only | `infrastructure/persistence/database.py` + PR #9 |
| **`Specialty` search is Spanish-only with a 6-tier scoring algorithm** — English aliases deferred to the LLM matcher (#15) | `domain/value_objects/specialty.py::SpecialtyCatalog.search` |
| **Issue-per-branch GitHub flow** — never commit feature work directly to `main`/`dev`; branch name = `<issue#>-<kebab-title>` | personal memory + this repo's PR history |
| **No real ForeignKey constraints in SQLite (yet)** — logical references only, app-level discipline. FKs added with Postgres move. | `docs/schema.dbml` header comment |
| **Pure dependency injection in `main.py`** (no DI framework) — constructor injection, factory pattern, testable | `main.py` |

## How this document gets updated

This is a **lightweight, periodic update** — not synced to every PR. Targets:

- After each phase transitions (current focus changes)
- When a major architectural decision is made (add a row to the table above)
- Roughly weekly during active development; less often when in maintenance

Issue-level state lives on GitHub. This doc is the meta-summary.

## Honest MVP timeline

If we stay focused on the next-steps order — [#18](https://github.com/AntonellaMasini/medagent/issues/18) → [#3](https://github.com/AntonellaMasini/medagent/issues/3) → [#4](https://github.com/AntonellaMasini/medagent/issues/4) → [#5](https://github.com/AntonellaMasini/medagent/issues/5) — a functional end-to-end MVP is probably **1-2 weeks of focused work**. Most of the variance is in [#4](https://github.com/AntonellaMasini/medagent/issues/4) (real voice calling with an LLM has historically been the kind of feature that surprises you in week 3).
