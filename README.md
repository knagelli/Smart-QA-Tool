# Smart QA Tool — Phase 1 Web App

Requirements file (Word / Excel / PDF) + application name in → requirement
validation, test scenario generation, and a Req ID ↔ Test Case ID
traceability matrix out, as an HTML report and an Excel workbook.

Clients use this through a browser. **They never need a Claude account or
API key** — your Anthropic API key lives only in this server's
environment.

## How it works
1. Client opens the web page, enters an access code (you issue one per
   client), types the application name, uploads the requirements file.
2. The server extracts the text (`app/extract.py`), sends it to Claude
   with your server-side API key (`app/qa_engine.py`), and gets back a
   validation + test-scenario JSON structure.
3. `app/report_builder.py` renders that JSON into the HTML report and the
   3-sheet Excel workbook (same logic as the chat-based tool, just called
   from a web request instead of a conversation).
4. The client gets a results page with links to both files.

## Run it locally
```bash
cd webapp
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: put your real ANTHROPIC_API_KEY and your client access codes

export $(cat .env | xargs)
uvicorn app.main:app --reload --port 8000
```
Visit http://localhost:8000

## Client access codes (manual onboarding, no signup flow)
Set `CLIENT_ACCESS_CODES` as `client_name:code,client_name2:code2`. Add a
new pair and restart the server to onboard a client — no database, no
billing integration, matches "handful of clients, I'll onboard them
myself" for this phase. Every run is appended to
`app/runs/run_log.csv` (run id, client, timestamp, application, filename)
so you can see usage per client for invoicing.

**If `CLIENT_ACCESS_CODES` is unset, the app allows anyone with the URL
to run analyses.** That's fine for local testing only — always set real
codes before deploying anywhere reachable by clients.

## Deploying
This is a standard FastAPI/Uvicorn app — any host that runs Python ASGI
apps works:
- **Render / Railway / Fly.io** — point at this repo, set the two env
  vars (`ANTHROPIC_API_KEY`, `CLIENT_ACCESS_CODES`) as secrets in their
  dashboard, start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- **Your own VM** — run behind nginx/Caddy with TLS, same env vars,
  ideally under a process manager (systemd, supervisor) so it restarts on
  crash/reboot.

Put it behind HTTPS wherever you deploy — access codes and requirements
documents travel over that connection.

## Security notes for a real deployment
- `ANTHROPIC_API_KEY` must be a real secret (host's secret manager /
  env var), never committed to source control (`.gitignore` already
  excludes `.env`).
- Uploaded files and generated reports currently live under
  `app/runs/<run_id>/` on local disk. For anything beyond a handful of
  clients, add a cleanup job (delete runs older than N days) or move to
  object storage (S3-compatible) — this MVP doesn't do either yet.
- Access codes are a simple shared-secret gate, not real authentication —
  fine for a handful of trusted clients you onboard yourself; revisit if
  this grows (proper per-user login, rate limiting per client, etc.).
- Requirements documents may contain business-sensitive content. Decide
  and tell clients your retention policy (e.g. "reports auto-delete after
  7 days") — currently nothing auto-deletes; that's a follow-up.

## Not in this phase
Live test execution against a client's actual application (Salesforce,
Humanforce, SuccessFactors, etc.) is **Phase 2**, deliberately left out
of this build. It needs its own hosted browser-automation layer and a
different credential-handling model (per-run, client-entered, in-memory
only) — see the project notes for the design discussion before that
phase starts.

## Files
```
webapp/
  requirements.txt
  .env.example
  README.md
  app/
    main.py            FastAPI routes: /, /analyze, /download, /healthz
    extract.py          .docx / .xlsx / .pdf / .txt -> plain text
    qa_engine.py         builds the Claude prompt, calls the API, parses JSON
    report_builder.py    JSON -> HTML report + Excel workbook
    templates/           index.html (upload form), result.html (download links)
    static/style.css
    runs/                per-run output + run_log.csv (created at runtime)
```
