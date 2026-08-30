# Mise

An instrumented kitchen. A recipe screen driven by a load cell, a pantry that
tracks its own consumption, and a log that turns every meal into a data point.

Built to be measured, not just used: every ingredient added is recorded as
target-versus-actual, so the whole thing doubles as a process-capability study
on the person operating it.

---

## Quick start — laptop, five minutes, no hardware

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # optional: only needed for the assistant
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. From your phone on the same WiFi, use your
laptop's LAN address. API docs are at `/docs`.

**Start logging cooks in manual mode now, before any hardware exists.** Manual
entry is not a placeholder — it is the control group. The manual-versus-scale
comparison is the strongest evidence the rig was worth building, and it only
works if manual data exists first. Twenty manual cooks is twenty data points
you cannot go back and collect later.

## Raspberry Pi

```bash
git clone <your-repo> ~/mise && cd ~/mise
bash scripts/install-pi.sh
```

That installs a venv, registers a systemd service, and starts it on boot. The
Pi is then reachable at `http://mise.local:8000` from anything on the network —
set the hostname to `mise` in Raspberry Pi Imager when you flash the card.

Touchscreen setup is in `scripts/kiosk.md`. Do it last.

---

## Architecture

```
browser (web/index.html)          one page, synchronous, no framework
   │
   ├── web/api.js                 the only thing that talks to the network
   │
   ▼
FastAPI (server/main.py)
   ├── server/db.py               SQLite. one file, WAL, atomic ops
   ├── server/assistant.py        recipe import + adaptation
   └── server/scale.py            HX711, simulated off-Pi
```

### Why SQLite and not a spreadsheet

The scale writes during a cook while your phone reads the same pantry. A
spreadsheet loses that race and there is no way to detect that it did. SQLite in
WAL mode handles it, and CSV import/export (`/api/pantry.csv`, `/api/cooks.csv`)
gives you the spreadsheet whenever you actually want one.

### Concurrency

Whole-state writes carry a `rev`. A stale rev returns **409**, the client
refetches, replays its local settings, and retries once. That is optimistic
concurrency control, and it is the reason two devices editing the pantry cannot
silently overwrite each other.

Two things deliberately bypass that path because they must never be lost:

| Endpoint | Why it is separate |
|---|---|
| `POST /api/cooks` | Append-only. A logged cook is data you cannot recreate. |
| `PATCH /api/pantry/{id}` | Single-row read-modify-write under a lock. What the scale and the intake flow use. |

### Getting a key — free is fine

The assistant does one small job (messy text → structured JSON), so it does not
need an expensive model. Set **one** key in `.env` and Mise detects which
provider you meant:

| Provider | Key | Free? |
|---|---|---|
| Google Gemini | `GEMINI_API_KEY` — [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | free tier, no card |
| Groq | `GROQ_API_KEY` — [console.groq.com/keys](https://console.groq.com/keys) | free tier, no card |
| Cerebras | `CEREBRAS_API_KEY` | free tier, no card |
| OpenRouter | `OPENROUTER_API_KEY` | free models, low daily cap |
| Anthropic | `ANTHROPIC_API_KEY` | paid |
| OpenAI | `OPENAI_API_KEY` | paid |

All of them except Anthropic speak the OpenAI chat-completions shape, so they
share one code path — adding another is one row in `PROVIDERS`
(`server/assistant.py`). `GET /api/assist/health` reports which one is live.

Free tiers have daily caps. That is fine here: importing a recipe is a handful
of calls a week, not a service under load.

### Where the model is allowed to be

The assistant does exactly one job: **unstructured → structured, at the edges.**

It never sees a scale factor, never converts a unit, never picks a pantry id,
never touches a number that ends up in your logs. It returns ingredient names
and amounts as written; Python does the conversion and the matching. Its output
is validated against `RecipeDraft` and rejected if it doesn't fit — a negative
quantity fails the schema before it reaches the UI.

That separation is the point. Everything reproducible stays in code, where it
can be tested; the model handles only the genuinely fuzzy part.

### The assistant proposes; you approve; Python applies

`POST /api/assist/chat` is the only conversational endpoint, and it **cannot
write**. It reads a snapshot of the pantry and returns a `Proposal` — a typed
change-set. The browser renders that as a diff. Only your click turns it into
real writes, and those replay through the ordinary endpoints (`PATCH
/api/pantry/{id}`, `PUT /api/state`) that your own clicks use.

So there is one write path in this system, not two, and it is the audited one.

Four things happen to a proposal before you ever see it:

| Guard | What it stops |
|---|---|
| `Proposal` schema validation | a malformed change-set is discarded whole, never half-applied |
| `PantryOp.field` allow-list | it can set `expires` or `shelf`; it cannot set `qty` through the back door |
| id existence check | hallucinated pantry ids are dropped and the drop is reported |
| non-JSON reply | shown to you as text, proposing nothing |

The worst a confused model can do here is waste your time. It cannot quietly
change a number you never looked at. Verified: `tests/` exercises all four.

### Import tiers

Cheapest and most reliable first:

| Tier | Source | Model call? |
|---|---|---|
| 1 | `schema.org/Recipe` JSON-LD embedded in the page | no |
| 2 | YouTube auto-subtitles via `yt-dlp` (transcript only, no video) | yes |
| 3 | Stripped page text | yes |
| 4 | Text you pasted | yes |
| 5 | Local regex parser | no |

Most real recipe sites publish JSON-LD, so tier 1 handles them perfectly for
free. Tier 5 means the import form still works with no API key configured — it
just tells you honestly that it's guessing.

**Instagram, Pinterest, Facebook and TikTok block server fetches.** There is no
clever way around it that doesn't mean parking your login on the Pi. Mise tries
yt-dlp once (it sometimes gets a public reel), then tells you plainly to paste
the caption. That path works every time and costs about ten seconds.

Pages holding several recipes — round-ups, "12 weeknight pastas" — return a
candidate list instead of silently importing the first one.

---

## API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/state` | Everything. Includes `rev`. |
| `PUT` | `/api/state` | Whole-state write. 409 on stale `rev`. |
| `POST` | `/api/seed` | One-shot import of the browser prototype's blob. |
| `PATCH` | `/api/pantry/{id}` | Atomic `delta` or `absolute`. |
| `POST` | `/api/cooks` | Append a cook; decrements the pantry per ingredient. |
| `GET` | `/api/pantry.csv`, `/api/cooks.csv` | Export. |
| `GET` | `/api/barcode/{code}` | Your pantry first, then Open Food Facts. |
| `POST` | `/api/import` | URL or text → reviewed draft. |
| `POST` | `/api/assist/adapt` | "make it vegetarian" → a proposed diff. |
| `GET` | `/api/assist/health` | Which tiers are actually available. |
| `GET` | `/api/scale` | Current grams + stability. |
| `POST` | `/api/scale/tare`, `/api/scale/calibrate` | Calibration. |
| `WS` | `/ws/scale` | ~10 Hz live weight for the cook screen. |

---

## The scale

`server/scale.py` runs simulated on a laptop and real on the Pi behind the same
interface, so the UI never knows the difference and you can build everything
before the hardware lands.

**Pi 5 note:** `RPi.GPIO` does not work on a Raspberry Pi 5 — GPIO moved behind
the RP1 southbridge and RPi.GPIO's SOC base-address probe fails. Install
`rpi-lgpio` instead; it is a drop-in that registers itself as `RPi.GPIO`, so no
code changes. Install it only when the cell is actually wired:

```bash
./.venv/bin/pip install hx711 rpi-lgpio
```

Wiring (HX711 → Pi 5):

| HX711 | Pi |
|---|---|
| VCC | 5 V (pin 2) |
| GND | GND (pin 6) |
| DT | GPIO 5 (pin 29) |
| SCK | GPIO 6 (pin 31) |

Calibrate:

1. Empty pan → `POST /api/scale/tare`
2. Put a known mass on → `POST /api/scale/calibrate {"grams": 500}`

Stored in `data/scale.json`.

**Stability detection** is standard deviation over a short window, not a bare
threshold and not a fixed delay. A threshold fires while the pan is still
moving; a delay is slower than it needs to be. This is the piece worth writing
up properly — it's a real signal-processing decision with a defensible reason.

Load cells drift with temperature. A hot pan on the platform will read wrong.
Tare often, and don't design a workflow that assumes a cold start.

## The tare table

Don't try to weigh contents separately from packaging. Open Food Facts returns
net quantity for most EU products, so:

```
packaging tare = gross you weigh − net from the barcode
```

Measure once per packaging type, store it on the pantry row, reuse forever. For
anything you decant into your own jar, weigh the empty jar once and save that as
the container's tare. It is the same table a filling line keeps.

---

## Roadmap

- [ ] microSD card (**blocker** — the Pi will not boot without one; A2-rated 64 GB)
- [ ] Server on the Pi, phone + laptop pointed at it
- [ ] 20 manual cooks logged — the control group
- [ ] `ANTHROPIC_API_KEY` set, import from a link working
- [ ] PWA manifest + share target so Instagram can hand off to it
- [ ] HX711 + 5 kg load cell wired, calibrated, auto mode real
- [ ] Second low-range cell for salt and yeast
- [ ] Barcode scanning from the phone camera (ZXing fallback for iOS Safari)
- [ ] Modal survey of the scale platform, then the isolation mount and filter
- [ ] DSI touchscreen in kiosk mode
