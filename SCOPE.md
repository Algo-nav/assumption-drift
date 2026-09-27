# Claude Code Prompt: Assumption Drift Dataset + Research Record Schema

You are building a public, open-source project. Read this whole file before writing any code. Everything outside the SCOPE FENCE section is out of bounds.

**Working directory:** `/Users/macintoshhd/Desktop/Renaissance/assumption-drift`
All paths in this file are relative to that directory. Do not create or modify files anywhere else on this machine.

**Fixed constants (use exactly, do not ask):**
- EDGAR User-Agent: `Navneet navn07588@gmail.com`
- Reviewer name in records: `navneet`
- Hugging Face and GitHub username: to be supplied by Navneet before Phase 3; leave as `{HF_USER}` placeholder in `config.yaml` until then

---

## 0. What this is

Two things, one repo:

1. **`assumption-drift`**: a Hugging Face dataset. Rows are numeric forward guidance statements from public companies, taken from their own SEC filings, paired with what the company later reported for that same metric and period, and whether the company ever acknowledged the gap.
2. **`research-record`**: a thin Python package. A Pydantic schema for what a written-down research conclusion should minimally contain, plus a CLI validator. The dataset rows are the schema's example corpus.

Purpose: a problem statement in data form. Not a product, not a demo, not a model.

Licences: dataset CC-BY-4.0, code MIT. Published under Navneet's personal Hugging Face and GitHub accounts.

---

## 1. Repo layout

```
/Users/macintoshhd/Desktop/Renaissance/assumption-drift/
├── README.md                     # repo readme, short, links to the HF dataset card
├── LICENSE                       # MIT (code)
├── pyproject.toml                # package: research_record, CLI entry point: rr
├── research_record/
│   ├── __init__.py
│   ├── schema.py                 # Pydantic models (Phase 0)
│   ├── rubric.py                 # resolution rules as pure functions (Phase 0)
│   ├── validate.py               # validator logic
│   └── cli.py                    # `rr validate`, `rr stats`
├── pipeline/
│   ├── config.yaml               # company list, date range, throttle, budget cap
│   ├── 01_fetch.py               # EDGAR fetch + local cache
│   ├── 02_candidates.py          # regex pass: sentences with guidance verbs + numbers
│   ├── 03_structure.py           # Haiku batch pass: candidate sentence -> draft row
│   ├── 04_outcomes.py            # find later filing reporting the same metric/period
│   ├── 05_review.py              # writes review queue; only approved rows go forward
│   └── 06_publish.py             # parquet + jsonl + card, upload to HF
├── data/
│   ├── raw/                      # cached filings, gitignored
│   ├── review/                   # review queue CSVs, human-edited, committed
│   └── release/                  # final parquet + jsonl, committed
├── card/
│   └── README.md                 # Hugging Face dataset card
└── tests/
    ├── test_schema.py
    ├── test_rubric.py
    ├── test_validate.py
    └── test_provenance.py
```

---

## 2. Phase 0: schema and rubric (Opus session, do this first, stop after)

### 2.1 `research_record/schema.py`

Keep it minimal. Every field earns its place or gets cut.

```python
class Evidence(BaseModel):
    source_url: HttpUrl          # must be sec.gov
    accession_number: str        # 0000000000-00-000000 format
    filing_type: str             # 10-K, 10-Q, 8-K
    filed_at: date
    fetched_at: datetime
    content_sha256: str          # hash of the fetched document
    excerpt: str                 # the exact sentence(s), max 400 chars

class Assumption(BaseModel):
    text: str
    metric: str                  # e.g. "revenue", "non-GAAP EPS", "gross margin"
    target_low: float | None
    target_high: float | None
    unit: str                    # "USD", "USD millions", "percent", "units"
    target_period: str           # "FY2024", "Q3 2024"
    stated_at: date
    evidence: Evidence

class Outcome(BaseModel):
    reported_value: float | None
    reported_at: date
    evidence: Evidence

class ResearchRecord(BaseModel):
    record_id: str               # ulid
    company: str
    ticker: str
    cik: str
    claim: str                   # plain restatement of the guidance
    assumption: Assumption
    outcome: Outcome | None
    invalidation_condition: str  # "reported value falls outside [low, high]"
    status: Literal["open", "met", "missed", "withdrawn", "unresolved"]
    acknowledged_at: date | None # first later filing that references the miss
    acknowledgement_evidence: Evidence | None
    days_to_falsifiable: int | None
    days_to_acknowledged: int | None
    last_reviewed_at: date
    reviewer: str                # "navneet"
```

Not in the schema, do not add: cross-record links, contradiction fields, confidence scores, any free-text judgment field beyond `claim`.

### 2.2 `research_record/rubric.py`

Resolution is mechanical. No judgment calls.

- **met**: `target_low <= reported_value <= target_high` (point guidance: low == high, tolerance 0.5% of value)
- **missed**: reported_value outside the range
- **withdrawn**: company explicitly withdrew or suspended guidance in a later filing before the period closed
- **unresolved**: period has not closed, or no later filing reports the metric
- **open**: draft row, not yet reviewed

Out of scope: guidance without an explicit number and period. If it says "strong growth" it is not a row.

`days_to_falsifiable` = outcome.reported_at minus assumption.stated_at.
`days_to_acknowledged` = acknowledged_at minus outcome.reported_at, null if never acknowledged.

Write `tests/test_rubric.py` with edge cases: boundary values, point guidance, withdrawn before close, outcome after period but before filing.

**Stop here. Report back. Sonnet takes over from Phase 1.**

---

## 3. Phase 1: EDGAR fetch and candidates (Sonnet)

### 3.1 `pipeline/01_fetch.py`

- Source: SEC EDGAR full-text search (`efts.sec.gov/LATEST/search-index`) and filing archives.
- Every request carries these headers, read from `config.yaml`:
  ```python
  headers = {
      "User-Agent": "Navneet navn07588@gmail.com",
      "Accept-Encoding": "gzip, deflate",
      "Host": "www.sec.gov",   # "efts.sec.gov" for full-text search calls
  }
  ```
- Hard throttle at 8 requests/second using a token-bucket limiter, not `sleep()`. SEC limit is 10/second per IP; exceeding it blocks the IP for roughly 10 minutes.
- Smoke test before anything else runs (also in `tests/test_provenance.py`): fetch `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&type=10-K&output=atom` with the header and expect 200; without the header expect 403.

`config.yaml` starting block:
```yaml
edgar:
  user_agent: "Navneet navn07588@gmail.com"
  max_rps: 8
  filing_types: ["8-K", "10-K", "10-Q"]
  date_from: "2019-01-01"
  date_to: "2025-12-31"
companies: []            # Navneet fills: [{name, ticker, cik}], start with 3
llm:
  model: "claude-haiku-4-5"
  budget_usd: 10
hf:
  user: "{HF_USER}"
  dataset: "assumption-drift"
```
- Filing types: 8-K exhibit 99.1 (earnings releases), 10-K, 10-Q. Date range from `config.yaml`.
- Cache every fetched document under `data/raw/{cik}/{accession}.html`. Store `fetched_at`, final URL after redirects, HTTP status, and sha256 in a sidecar `.meta.json`. **Only URLs that returned 200 enter any row.**
- Idempotent: re-running skips cached files.

### 3.2 `pipeline/02_candidates.py`

- Strip HTML to text. Split to sentences.
- Candidate = sentence containing (a) a forward-looking verb or noun from a list in `config.yaml` (`expects`, `guidance`, `outlook`, `anticipates`, `forecast`, `targets`, `full year`, `fiscal 20xx`) and (b) a number with unit or percent.
- Output `data/candidates/{cik}.jsonl` with sentence, char offsets, accession, filing type, filed date.

### 3.3 Company list

Start with 3 companies in `config.yaml` to prove the pipeline end to end. Criteria for the full list of 30: US-listed, gives numeric annual or quarterly guidance in earnings releases, at least 5 years of filings, mix of sectors (retail, semis, SaaS, industrials, consumer). Navneet supplies the final 30.

---

## 4. Phase 2: structure, outcomes, review (Sonnet)

### 4.1 `pipeline/03_structure.py`

- Model: **claude-haiku-4-5** via the **Message Batches API**. Do not use Sonnet or Opus here.
- Input: candidate sentence + 2 sentences of context. Output: draft `Assumption` JSON or `null` if the sentence is not numeric guidance with an explicit period.
- Budget cap in `config.yaml` (default USD 10). Count input tokens before submitting; abort if projected cost exceeds cap.
- Every output is a **draft**. Nothing from this step is trusted until Phase 2.3.

### 4.2 `pipeline/04_outcomes.py`

- For each draft assumption, search the same company's later filings for the first one reporting the same metric for the same period. Same regex + Haiku approach, same cap.
- Then search filings after the outcome for acknowledgement: mentions of the metric alongside words like `below`, `short of`, `did not meet`, `lower than our`, `revised`. Draft `acknowledged_at` if found.

### 4.3 `pipeline/05_review.py`

- Writes `data/review/{cik}.csv` with every draft row, all fields, plus columns `approved` (bool, default false), `reviewer_note`.
- Navneet opens the CSV, checks each row against the cached filing, sets `approved`, edits fields where the model got it wrong.
- **Provenance rule:** Navneet independently opens EDGAR and hand-verifies at least 10% of approved rows per company, marking `hand_verified=true`. Any mismatch between the pipeline URL and the hand-found URL fails the whole company batch.
- Only `approved=true` rows proceed. Unapproved rows are never published.

---

## 5. Phase 3: validator and publish (Sonnet)

### 5.1 `rr validate <path>`

Exit non-zero on any failure. Checks:
- Every row parses as `ResearchRecord`
- Every `source_url` host is `www.sec.gov` or `efts.sec.gov`
- Accession number format
- `stated_at <= filed_at`, `outcome.reported_at > assumption.stated_at`, `acknowledged_at >= outcome.reported_at`
- `status` matches what `rubric.resolve()` returns from the values
- `content_sha256` matches the cached file
- Excerpt appears verbatim in the cached document

### 5.2 `rr stats <path>`

Prints: row count, per-status counts, median `days_to_falsifiable`, share of misses never acknowledged, per-company breakdown. This output feeds the LinkedIn posts, so make it clean.

### 5.3 `pipeline/06_publish.py`

- Writes `data/release/assumption_drift.parquet` and `.jsonl`.
- Renders `card/README.md` with live numbers from `rr stats`.
- Uploads to `hf://datasets/{HF_USER}/assumption-drift` via `huggingface_hub`, reading `hf.user` from `config.yaml`. Dry-run flag default on. Refuse to run live if `hf.user` is still the placeholder.

### 5.4 Dataset card content (public-facing, register enforced)

Sections in order: what this is (3 sentences), how a row is built, the resolution rubric, provenance guarantee, known limitations, how to cite, licence. Plain words. No em-dashes. No product or company mention. Do not use the phrase "buy-side". Describe the problem, not a solution.

### 5.5 Figures (`card/figures/`)

Four charts, written by `pipeline/06_publish.py` from release data. Matplotlib only. Never hand-edited: a figure that needs to change changes because the release data or this code changed, not because someone opened it in an editor.

1. **Falsifiable vs acknowledged.** Scatter of `days_to_falsifiable` (x) against `days_to_acknowledged` (y), missed rows only, y on a log scale. A missed row that was never acknowledged has no `days_to_acknowledged` to plot on a log axis, so those rows are not dropped: they are drawn as a strip along the top of the chart, at the same x position their `days_to_falsifiable` gives them.
2. **Acknowledgement by company.** Horizontal bars: share of shortfalls (missed rows whose reported value fell below the guided range, `research_record.rubric.direction`) that were never acknowledged, one bar per company.
3. **Resolution by fiscal year.** Stacked bars of met / missed / withdrawn counts, one stack per fiscal year (the year named in `assumption.target_period`, quarter or full year alike).
4. **Miss magnitude.** Histogram, missed rows only, of `(reported_value - nearest target edge) / nearest target edge`: the nearest edge is `target_high` for a beat, `target_low` for a shortfall, or the point value itself for point guidance.

Colour: neutral greys, plus one accent colour reserved for "missed". Never red, amber or green anywhere in any figure: this is a dataset of gaps, not a scorecard, and stoplight colours read as a verdict this project is not making.

Every figure carries one footer line, in the reserved neutral grey: the dataset name, the row count behind that figure, the generation date, and "Source: SEC EDGAR".

`tests/test_figures.py` asserts all four files exist under `card/figures/` and are non-empty after a dry run of `pipeline/06_publish.py`: figures are written on every run, live or dry, since only the Hugging Face upload is gated on `--live`.

---

## 6. Tests and verification gate

All of these must pass before any phase is called done:

```
pytest tests/ -q
rr validate data/release/assumption_drift.jsonl
python -m pipeline.06_publish --dry-run
```

`tests/test_provenance.py`: pick 5 random release rows, re-fetch each `source_url` live, compare sha256 to stored hash, assert excerpt present. This test hits the network and is marked `@pytest.mark.network`.

Report back with the three command outputs pasted verbatim.

---

## 7. SCOPE FENCE

Do not:
- Build any UI, Gradio Space, or web viewer
- Add contradiction detection, cross-record linking, or any field comparing one record to another
- Train, fine-tune, or evaluate any model
- Use any model other than claude-haiku-4-5 inside the pipeline
- Touch any other repository or any file outside this repo
- Add SCND Order names, links, colours, or fonts anywhere
- Publish any row that is not `approved=true`
- Construct a URL that was not fetched with a 200 response
- Expand the company list beyond `config.yaml` without being told
- Add fields to the schema beyond section 2.1 without being told

If a task seems to need one of these, stop and say so in one sentence.

---

## 8. Time box

One week of evenings. Phase 0 one session. Phase 1 on 3 companies before touching Phase 2. If Phase 2 is not working on 3 companies by day 4, stop and report.
