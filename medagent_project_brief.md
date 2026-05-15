# MedAgent — Project Brief for Claude Code
 
## What we're building
 
An AI agent that lets users book private health insurance appointments in Spain via WhatsApp. The user sends a message like "book me an appointment with a psychologist" and the agent handles everything: logs into their insurer's website, finds available doctors sorted by proximity, calls each clinic via AI voice until one confirms a slot that fits the user's calendar, then sends a WhatsApp confirmation back.
 
This is being built open source, starting with Cigna Spain as the first insurer adapter.
 
---
 
## The problem it solves
 
In Spain, private health insurance (e.g. Cigna, Adeslas, Sanitas) requires users to:
1. Go to the insurer's website and search for doctors in their specialty
2. Note down each clinic's phone number
3. Call each one manually until they find availability
4. Check their own calendar
5. Confirm and remember the appointment
Some clinics don't answer, some don't have the specialty despite appearing in the directory, some have two-month waits. The whole process can take 30–60 minutes of phone calls. This agent automates it entirely.
 
---
 
## Tech stack decisions
 
| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Async support, rich ecosystem |
| Web framework | FastAPI | Async, webhook-friendly |
| Browser automation | Playwright (async) | Handles modern SPAs, good Python API |
| WhatsApp | Twilio WhatsApp API | Easiest setup for MVP, webhook-based |
| AI voice calls | Twilio Voice + ElevenLabs Conversational AI (or GPT-4 Realtime) | Outbound calls, real-time LLM conversation |
| Calendar | Google Calendar API | User pre-authorizes read access |
| Maps / distance | Google Maps Geocoding + Distance Matrix API | Sort clinics by walking/driving distance |
| Session storage | JSON files (MVP) → Redis later | Store Cigna browser cookies between runs |
| User profile storage | SQLite (MVP) → Postgres later | Store address, credentials, preferences |
| Orchestration | LangGraph or simple async Python | Coordinate tools, handle retries |
 
---
 
## System architecture
 
```
User (WhatsApp)
    ↓ message
Twilio webhook → FastAPI /webhook endpoint
    ↓
Orchestrator agent
    ├── Cigna scraper (Playwright)
    │     └── Returns: list of doctors with name, address, phone, distance
    ├── Distance sorter (Google Maps API)
    │     └── Returns: same list sorted by distance from user's home
    ├── Calendar checker (Google Calendar API)
    │     └── Returns: user's available slots + constraints
    └── AI voice caller (Twilio Voice + LLM)
          └── Calls each clinic in order until confirmed booking
    ↓
Result aggregator → picks best confirmed slot
    ↓
WhatsApp confirmation → sent back to user via Twilio
```
 
---
 
## User profile (stored on first setup)
 
```python
{
  "phone": "+34612345678",          # their WhatsApp number
  "name": "Antonella Masini Ortiz",
  "home_address": "Calle de Donoso Cortés 20, Chamberí, Madrid",
  "insurer": "cigna",
  "insurer_nie": "YBLL3377I",       # stored encrypted
  "insurer_password": "...",         # stored encrypted
  "calendar_constraints": {
    "preferred_times": "afternoons", # mornings / afternoons / any
    "excluded_days": ["saturday", "sunday"]
  },
  "google_calendar_token": "..."    # OAuth token
}
```
 
---
 
## Cigna scraper — detailed flow
 
The agent uses Playwright to control a headless Chromium browser.
 
### Step 1: Login
- URL: `https://clientes.cigna.es`
- Fill `input[placeholder="NIE, NIF, Pasaporte"]` with user's NIE
- Fill `input[placeholder="Contraseña actual"]` with password
- Click `button:has-text("Acceder")`
### Step 2: OTP verification (the hard part)
Cigna sends a verification code via SMS to the user's registered phone.
 
**Strategy — session cookie reuse first:**
- On first login, save browser cookies to disk after successful auth
- On subsequent runs, load saved cookies and navigate directly to the cuadro médico page
- Check if the page loaded correctly (URL contains `/cp/cuadro-medico`) — if yes, skip login entirely
- If cookies expired, trigger full login + OTP flow
**OTP flow when cookies are expired:**
- Agent detects it's on the verification screen
- Sends user a WhatsApp: "Cigna needs to verify you. Check your SMS and reply with the code."
- Agent pauses and waits for user's WhatsApp reply (polling the Twilio conversation)
- Types the received code into the OTP field
- Saves fresh cookies after successful verification
### Step 3: Navigate to doctor search
- Navigate to `https://clientes.cigna.es/cp/cuadro-medico`
- The search form has:
  - Localización field (text input with autocomplete)
  - Especialidad field (text input with autocomplete dropdown)
  - "Cerca de" radio button (proximity search)
  - "Buscar" button
### Step 4: Fill search form
```python
# Set location from user profile
await page.click('input[type="radio"][value="near"]')  # select "Cerca de"
await page.fill('input[placeholder*="lugar"]', user.home_address)
 
# Type specialty and select from autocomplete
await page.fill('input[placeholder*="especialidad"]', specialty_query)
await page.wait_for_selector('.autocomplete-dropdown')
await page.click(f'.autocomplete-option:has-text("{specialty_normalized}")')
 
# Click search
await page.click('button:has-text("Buscar")')
await page.wait_for_selector('.doctor-card')
```
 
**Specialty normalization:** The user says "psychologist" → agent maps to "PSICOLOGIA". Need a mapping dict for common specialties (dermatologist → DERMATOLOGÍA, etc.)
 
### Step 5: Parse results
The results page (`/cp/cuadro-medico`) shows doctor cards. Each card contains:
- Doctor/clinic name
- Distance in meters
- Address
- Phone number (visible directly on card — e.g. `722736733`)
- Ratings
```python
doctors = []
cards = await page.query_selector_all('.doctor-card')
for card in cards:
    doctors.append({
        'name': await (await card.query_selector('.doctor-name')).inner_text(),
        'distance_m': parse_distance(await (await card.query_selector('.distance')).inner_text()),
        'address': await (await card.query_selector('.address')).inner_text(),
        'phone': await (await card.query_selector('.phone')).inner_text(),
    })
```
 
**Important:** The site has a "Ordenar por → Cercanía" dropdown. Click this before scraping so results come pre-sorted by distance.
 
---
 
## AI voice caller — how it works
 
Once we have a sorted list of doctor phones, the agent makes outbound calls using Twilio Programmable Voice + a real-time LLM.
 
### The call flow
1. Twilio dials the clinic's number
2. When someone picks up, a TwiML webhook fires to our FastAPI server
3. FastAPI starts a real-time LLM session (ElevenLabs or GPT-4 Realtime via WebSocket)
4. The LLM conducts the conversation:
   - "Buenos días, llamo de parte de [Name] para solicitar una cita con [specialty]"
   - Handles back-and-forth: when is availability? morning or afternoon? which days?
   - Cross-references against user's calendar constraints
   - Confirms the appointment: "Perfecto, entonces quedamos el martes 20 a las 16:00"
5. LLM signals booking confirmed → saves appointment details
6. If no availability or no answer: mark as tried, move to next clinic
### Retry logic
- No answer after 4 rings → skip, try next
- "We don't have that specialty" → skip, mark as mismatch
- "Next available is in 2 months" → skip if more than 4 weeks out (configurable)
- After 3 failed clinics → send user a WhatsApp update: "Still looking, tried 3 clinics so far..."
- After all clinics exhausted → notify user with summary of what was found
---
 
## WhatsApp message handling
 
### Incoming message parsing
User can send:
- "Book me an appointment with a psychologist"
- "Necesito cita con dermatólogo"
- "Find me a cardiologist for next week"
The LLM parses intent and extracts: specialty, urgency, any time preferences mentioned.
 
### Outbound messages the agent sends
- Confirmation it received the request and is working on it
- OTP request (if auth session expired)
- Progress updates for long searches ("Tried 3 clinics, still looking...")
- Final confirmation: "✅ Appointment booked! Dr. Patricia Jimeno Rodríguez, psychologist. Tuesday May 20 at 16:00. C. Vallehermoso 20, Madrid. Phone: 722736733"
- Failure summary if nothing found
---
 
## Code architecture principles
 
Use Domain-Driven Design (DDD) with a clean layered architecture.
 
### The four layers
 
**Domain layer** (`domain/`) — pure Python, zero external dependencies. This is the heart of the app. Contains entities, value objects, and domain logic.
 
```
domain/
├── entities/
│   ├── user.py          # User, UserProfile
│   ├── appointment.py   # Appointment, AppointmentStatus
│   └── doctor.py        # Doctor, Clinic
├── value_objects/
│   ├── specialty.py     # Specialty enum + normalization ("psicólogo" → PSICOLOGIA)
│   ├── address.py       # Address with geocoding support
│   └── time_slot.py     # TimeSlot, AvailabilityWindow
└── repositories/
    ├── user_repository.py         # Abstract interface only
    └── appointment_repository.py  # Abstract interface only
```
 
**Application layer** (`application/`) — use cases. One class per user action. Orchestrates domain objects, calls repository interfaces. No FastAPI, no Playwright here.
 
```
application/
├── book_appointment.py      # BookAppointmentUseCase
├── setup_user_profile.py    # SetupUserProfileUseCase
└── handle_otp.py            # HandleOTPUseCase
```
 
**Infrastructure layer** (`infrastructure/`) — all the messy external stuff. Implements the repository interfaces from domain.
 
```
infrastructure/
├── scrapers/
│   ├── base_scraper.py
│   └── cigna_scraper.py
├── voice/
│   └── twilio_voice_caller.py
├── messaging/
│   └── twilio_whatsapp.py
├── persistence/
│   ├── sqlite_user_repository.py       # implements domain UserRepository
│   └── sqlite_appointment_repository.py
└── external/
    ├── google_maps.py
    └── google_calendar.py
```
 
**Interface layer** (`interfaces/`) — FastAPI routes and webhook handlers. Thin as possible — just parse the request and call a use case.
 
```
interfaces/
├── webhooks/
│   ├── whatsapp_webhook.py
│   └── voice_webhook.py
└── api/
    └── health.py
```
 
### Key principles to enforce
 
- **Dependency rule**: dependencies only point inward. Domain knows nothing about FastAPI or Twilio. Application knows nothing about SQLite or Playwright. Violations are bugs.
- **Repository pattern**: use cases talk to abstract repository interfaces (`UserRepository`), never directly to SQLite. The SQLite implementation lives in infrastructure and gets injected.
- **Value objects are immutable**: `Specialty`, `Address`, `TimeSlot` are frozen dataclasses. No setters.
- **Use cases are single-responsibility classes** with one public method (`execute()`). No god classes.
- **Dependency injection**: wire everything up in `main.py` using a simple container — no magic frameworks, just constructor injection.
### Example of what clean looks like
 
```python
# domain/entities/doctor.py
@dataclass(frozen=True)
class Doctor:
    id: str
    name: str
    specialty: Specialty
    clinic_name: str
    address: Address
    phone: str
    distance_meters: int | None = None
 
 
# domain/repositories/user_repository.py
from abc import ABC, abstractmethod
 
class UserRepository(ABC):
    @abstractmethod
    async def get_by_phone(self, phone: str) -> User | None: ...
 
    @abstractmethod
    async def save(self, user: User) -> None: ...
 
 
# application/book_appointment.py
class BookAppointmentUseCase:
    def __init__(
        self,
        scraper: BaseInsurerScraper,
        voice_caller: BaseVoiceCaller,
        calendar: BaseCalendarService,
        appointment_repo: AppointmentRepository,
        whatsapp: BaseMessagingClient,
    ):
        self._scraper = scraper
        self._voice_caller = voice_caller
        self._calendar = calendar
        self._appointment_repo = appointment_repo
        self._whatsapp = whatsapp
 
    async def execute(self, user: User, request: AppointmentRequest) -> Appointment:
        doctors = await self._scraper.find_doctors(request.specialty, user.address)
        slot = await self._voice_caller.book_first_available(doctors, user.constraints)
        appointment = Appointment.create(user, slot)
        await self._appointment_repo.save(appointment)
        await self._whatsapp.send_confirmation(user, appointment)
        return appointment
 
 
# main.py — wire everything up
def create_app() -> FastAPI:
    app = FastAPI()
 
    # Infrastructure
    db = SQLiteDatabase(settings.DATABASE_URL)
    user_repo = SQLiteUserRepository(db)
    appointment_repo = SQLiteAppointmentRepository(db)
    whatsapp = TwilioWhatsAppClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    scraper = CignaScraper(cookies_path=settings.COOKIES_DIR)
    voice_caller = TwilioVoiceCaller(settings.TWILIO_VOICE_NUMBER)
    calendar = GoogleCalendarService(settings.GOOGLE_CALENDAR_CLIENT_ID)
 
    # Use cases
    book_appointment = BookAppointmentUseCase(scraper, voice_caller, calendar, appointment_repo, whatsapp)
    setup_profile = SetupUserProfileUseCase(user_repo, whatsapp)
 
    # Routes
    app.include_router(whatsapp_webhook_router(book_appointment, setup_profile))
    app.include_router(voice_webhook_router(voice_caller))
 
    return app
```
 
---
 
## Full project structure
 
```
medagent/
├── main.py
├── config.py
│
├── domain/
│   ├── entities/
│   │   ├── user.py
│   │   ├── appointment.py
│   │   └── doctor.py
│   ├── value_objects/
│   │   ├── specialty.py
│   │   ├── address.py
│   │   └── time_slot.py
│   └── repositories/
│       ├── user_repository.py
│       └── appointment_repository.py
│
├── application/
│   ├── book_appointment.py
│   ├── setup_user_profile.py
│   └── handle_otp.py
│
├── infrastructure/
│   ├── scrapers/
│   │   ├── base_scraper.py
│   │   └── cigna_scraper.py
│   ├── voice/
│   │   └── twilio_voice_caller.py
│   ├── messaging/
│   │   └── twilio_whatsapp.py
│   ├── persistence/
│   │   ├── sqlite_user_repository.py
│   │   └── sqlite_appointment_repository.py
│   ├── external/
│   │   ├── google_maps.py
│   │   └── google_calendar.py
│   └── cookies/               # gitignored
│
├── interfaces/
│   ├── webhooks/
│   │   ├── whatsapp_webhook.py
│   │   └── voice_webhook.py
│   └── api/
│       └── health.py
│
├── .env.example
├── requirements.txt
└── README.md
```
 
---
 
## Environment variables needed
 
```bash
# Twilio
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
TWILIO_VOICE_NUMBER=
 
# Google
GOOGLE_MAPS_API_KEY=
GOOGLE_CALENDAR_CLIENT_ID=
GOOGLE_CALENDAR_CLIENT_SECRET=
 
# ElevenLabs (for voice)
ELEVENLABS_API_KEY=
 
# App
DATABASE_URL=sqlite:///./medagent.db
SECRET_KEY=                        # for encrypting credentials at rest
BASE_URL=https://your-ngrok-url    # for webhooks during local dev
COOKIES_DIR=./infrastructure/cookies
```
 
---
 
## MVP scope (build this first)
 
1. FastAPI server with Twilio WhatsApp webhook
2. User profile setup flow (first-time WhatsApp conversation to collect address, NIE, etc.)
3. Cigna scraper (Playwright) — login, cookie management, OTP via WhatsApp, search, parse results
4. Distance sorting (Google Maps)
5. Hardcoded voice script first (no real-time LLM yet) — plays a script and captures confirmed slot
6. WhatsApp confirmation message
Leave for v2:
- Real-time LLM voice conversation (ElevenLabs / GPT-4 Realtime)
- Google Calendar integration
- Multiple insurer adapters (Adeslas, Sanitas)
- Web dashboard for managing user profiles
---
 
## How to start in Claude Code
 
Install Claude Code if you haven't:
 
```bash
npm install -g @anthropic-ai/claude-code
```
 
Navigate to where you want the project, then run:
 
```bash
claude
```
 
Paste this entire document as your first message, followed by:
 
> "Let's build this. Start by scaffolding the full project structure following the DDD architecture, then set up the FastAPI server with the Twilio WhatsApp webhook endpoint. Then build the Cigna scraper. I'll provide API keys via a .env file as we go."
 
---
 
## Notes on open sourcing
 
- Credentials (NIE, passwords) must be encrypted at rest — never stored in plain text
- Cookie files must be in `.gitignore`
- Each insurer scraper should be a separate adapter so the community can contribute new ones
- The project should work for any Spanish private insurer eventually
- Long-term: could extend to other countries (Italy, LatAm)
 