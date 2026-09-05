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

### Ideas are not recipes

`scope: "ideas"` on the Decide tab answers "what should I make?" with 4-6
dishes — a name, why it suits what you have, what it uses, what you'd need to
buy, and a search string. **It never returns quantities or steps.**

That line is deliberate. A model is good at "this sounds like what you want and
you own most of it" and bad at "340 g". An invented quantity would flow into the
cook log and quietly corrupt the dataset the project exists to collect. So an
idea is a starting point: **Find a recipe** searches the web, **Import one**
opens the import screen with the dish prefilled as a hint, and the numbers come
from a real source through the tiered importer, where they can be reviewed.

### Nothing blocks a cook

Missing an ingredient, or short a pan, never prevents you starting. It can't:
the whole point is to log what actually happened, and "I made it with 140 g
instead of 200 g" is a data point, while "I didn't cook because the app said no"
is nothing. Short stock and short vessels are stated plainly on the brief screen
and the Begin button is never disabled.

Vessels are pickable per stage on that same screen, and the choice is saved back
to the recipe — you own what you own, and you shouldn't re-pick the bowl every
time you cook the same thing.

### Pantry matching: a wrong match is worse than no match

An unmatched row shows amber and you fix it in two seconds. A confidently
*wrong* one silently logs chicken stock as the chicken breast you weighed, and
poisons the dataset this whole project exists to collect. So `match_pantry` is
deliberately conservative:

- shared words carry the score, not string similarity — "green onion" and
  "yellow onion" are one character apart and different ingredients;
- a raw fuzzy ratio only wins alone when it is near-identical (≥ 0.86);
- names are singularised and stripped of preparation, so "1 Large Egg, whisked"
  and "Eggs" are the same shelf item;
- and if two names disagree on a **distinguishing word** — colour, part, form,
  salted vs unsalted — the match is refused outright however similar the strings
  look. Sharing a group is fine: two oils are both oils.

The threshold is 0.6. `tests/` pins the six real cases that used to match wrongly
and the eleven that must keep matching.

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

### Retries and fallback

Free tiers get busy. `503 This model is currently experiencing high demand` is a
queue, not a broken setup, and dropping to the regex parser because a server was
busy for two seconds is the wrong call. So every model call goes through
`call_provider`, which:

1. retries transient statuses (408, 429, 500, 502, 503, 504) up to three times,
   backing off 1.5s → 3s → 6s, honouring `Retry-After` when the server sends one
   (capped at 20s so an import can never hang);
2. then tries a smaller sibling model — flagship models are the contended ones,
   and `gemini-3.5-flash-lite` is usually free when `gemini-3.7-flash` is not;
3. and tells you in the warnings when a fallback answered, because silently
   using a different model than you configured is a lie by omission.

A **permanent** error is raised on the first attempt. A 404 will not fix itself
on the twelfth try, and retrying it just burns your quota.

### Thinking models and the empty answer

Gemini 3.x reasons before it replies, and **reasoning tokens come out of the
same budget as the answer**. Under a 3000-token cap the model can spend the lot
thinking and return an empty string — which surfaces as
`ValueError: model returned no JSON object` and looks exactly like a broken
setup. It isn't; it's a budget that was too small for a model that thinks.

Two things follow:

- `reasoning_effort` is set per model — `"none"` for Gemini 2.5 (which can turn
  thinking off) and `"low"` for 3.x (which cannot). Reading amounts off a recipe
  is extraction, not deliberation; the budget belongs in the output.
- The default budget is 8000 tokens, and an empty answer with
  `finish_reason: length` is retried **once** at four times that before moving on.

If every model still comes back empty, the error says so plainly and names what
was tried, because at that point the problem is the prompt or the provider, not
the model you picked.

### When the model doesn't answer

`GET /api/assist/health?probe=1` makes one tiny real call and reports exactly
what came back. `GET /api/assist/models` lists the model names your key can
actually see. Both exist because "model call failed" is useless — you cannot
tell a bad key from a retired model name from a rate limit without the
provider's own words, so those words are what gets shown:

```
Gemini returned HTTP 404 for model 'gemini-2.5-flash' —
models/gemini-2.5-flash is not found for API version v1beta
(that model name doesn't exist for this key)
```

The **Check the model connection** button in the import panel runs both, and on
a 404 it prints the list of models you could use instead.

Import never hard-fails on a model error: it falls back to the local parser and
tells you, in the warnings, why it had to.

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
