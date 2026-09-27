# Job Scout

I got tired of reading the same job listings every night, so I built something
to read them for me.

Job Scout goes out and finds job boards on its own, scrapes the listings off
them, scores each one against a profile of what I'm actually looking for, and
puts the results somewhere I can look at them. The only part left for me is
deciding whether to apply.

---

## How it works

There are two halves. The first answers "where do job listings even live?" and
the second answers "is this one worth my time?"

```
   sourcing                              job_scout
   ─────────                             ─────────
   BoardHunter                           scrapers
      ├── search rotation (SearXNG)         ↓
      ├── awesome-lists                  dedup / upsert
      └── directory crawl                   ↓
             ↓                           LLM scorer
      LLM verifier                          ↓
             ↓                            SQLite
      source registry  ──────────────────────┘
                                             ↓
                                      FastAPI → dashboard
```

**Finding boards.** Most job tools hardcode a list of sites to scrape. This one
goes looking instead. `BoardHunter` runs a few different discovery strategies,
dedupes whatever URLs come back, and hands each candidate to an LLM that
decides whether it's actually a job board worth scraping. Anything that passes
gets saved to a registry so it doesn't have to be rediscovered.

**Deciding what matters.** `app/profile.py` holds the things I care about —
roles I want, skills I actually have versus ones I'm still learning, a salary
floor, where I'm willing to live. A scoring agent reads each listing against
that and returns a score out of 100, which of my skills it matched, and why it
scored the way it did. It comes back as a structured object, not a paragraph I
have to parse.

Every run gets logged with counts, so I can see what happened and when.

---

## The architecture, and why it's shaped this way

The code splits into a **spine** and a set of **buildings**. The spine is the
stuff everything needs; buildings are individual features that sit on top.

### Spine

**`spine/models.py` is the part I'm happiest with.** It's a model router with
three tiers:

- `LOCAL` — Ollama on my own machine. Free, and the default for everything.
- `HEAVY` — Featherless, for bigger open-weight models.
- `FRONTIER` — NanoGPT, for when it genuinely needs a frontier model.

An agent asks for a tier by name and gets a model back. If that tier isn't
available — Ollama isn't running, an API key is missing, the network's down —
`get_model_with_fallback()` walks down a chain and uses the next thing that
works instead of crashing.

The reason it's built this way is cost. Everything defaults to local and free.
Paid tiers are something an agent has to explicitly ask for, so I can't
accidentally run up a bill by looping over a few hundred listings.

**`spine/storage.py`** is SQLModel over SQLite. Jobs are treated as things I
observed rather than things I own, so they're deduped on
`(source, source_job_id)` and never rewritten — scraping the same board twice
doesn't produce two rows. What *is* mutable is my relationship to the job:
`NOT_APPLIED → INTERESTED → APPLIED → INTERVIEWING → …`. Scores live in a
separate `Evaluation` table, one job to many evaluations, so re-scoring a
listing adds to its history instead of erasing it.

### Buildings

**`buildings/sourcing/`** finds and verifies boards. Each discovery strategy
implements the same `discover()` interface and registers itself, so adding a
new way to find boards means writing one file. Crawling goes through a
`PoliteFetcher` that keeps the request rate reasonable.

**`buildings/job_scout/`** does the scraping and scoring. `workflow.py` is the
only file that knows about the whole pipeline — the scrapers don't know scoring
exists, and the scorer doesn't know where listings came from. That separation
is deliberate. It's what lets me add a scraper without touching anything else.

**`api.py`** puts it behind FastAPI. Runs kick off as background tasks, with a
bit of in-process state so the UI can tell whether one's already going.

### Why self-hosted search

Board discovery searches through a **SearXNG instance running in Podman**
rather than a commercial search API. SearXNG queries a pile of upstream engines
and rotates between them, so no single engine sees all my traffic, my queries
don't leave the machine except to those engines, and there's no per-call bill
or rate limit to design around. Config's in `searxng-config/settings.yml`.

---

## Stack

Python 3.12+ with [uv](https://github.com/astral-sh/uv). Agents are
[Agno](https://github.com/agno-agi/agno). Models come from Ollama, Featherless,
or NanoGPT, all through OpenAI-compatible endpoints. FastAPI and Uvicorn for
the API, SQLModel and SQLite for data, pydantic-settings for typed config,
httpx and BeautifulSoup for scraping, SearXNG for search. The dashboard is a
separate Svelte app:
[job-scout-dashboard](https://github.com/LoofaCow/job-scout-dashboard).

---

## Running it

```bash
git clone https://github.com/LoofaCow/job-scout.git
cd job-scout
uv sync
cp .env.example .env
```

You don't need to pay for anything. If Ollama's running locally, that's enough:

```bash
ollama serve
ollama pull qwen2.5:7b-instruct
```

Add `FEATHERLESS_API_KEY` or `NANOGPT_API_KEY` to `.env` if you want the
heavier tiers. For board discovery you'll also want SearXNG up:

```bash
podman run -d --name searxng -p 8080:8080 \
  -v ./searxng-config:/etc/searxng:z docker.io/searxng/searxng:latest
```

Then:

```bash
uv run run.py          # http://127.0.0.1:8000
```

Board discovery has its own CLI:

```bash
uv run python -m app.buildings.sourcing --help
```

Edit `app/profile.py` before a real run — roles, skills, salary, location.
Everything downstream reads from it.

---

## API

| Method | Route | What it does |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/jobs/surfaced` | Jobs scoring above the threshold |
| `GET` | `/jobs/{job_id}` | One job and its latest evaluation |
| `POST` | `/jobs/{job_id}/status` | Update application status |
| `GET` | `/runs` | Recent runs |
| `POST` | `/scout/run` | Kick off a run |
| `GET` | `/scout/status` | Is one running? |

---

## Where it's at

Working: board discovery with LLM verification, the source registry, RemoteOK
scraping, dedup, profile-based scoring with structured output, run tracking,
and the API.

Still to do:

- More scrapers. The registry's built for it, RemoteOK is just the one I did
  first.
- Scheduled runs — `apscheduler` is in the dependencies, and
  `spine/scheduler.py` is still a stub.
- Drafting cover letters from the profile.
- Giving agents memory across runs.

---

Built for one user, which is me. Config is typed the whole way through, so a
bad `.env` fails at startup with something readable instead of a `KeyError`
buried six modules deep.

MIT licensed.
