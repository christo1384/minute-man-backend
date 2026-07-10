# Minute Man — Backend

This is the real engine that turns a meeting transcript into structured minutes
and exports them to PDF / Excel. It replaces the old mock `main.py`.

You can run and test the whole thing **without any API key** using the built-in
`demo` provider, then add a Claude or OpenAI key when you're ready for real
extraction.

---

## What's in here

| File | What it does |
|---|---|
| `main.py` | The web server. Endpoints: `/api/health`, `/api/minutes`, `/export/pdf`, `/export/excel`. |
| `prompts.py` | The instructions given to the AI (the HSWA hierarchy-of-controls rules). Outputs JSON. |
| `llm.py` | Talks to Claude or OpenAI (or the keyless `demo` extractor). |
| `export_routes.py` | Builds the PDF and Excel files. |
| `requirements.txt` | The Python packages needed. |
| `.env.example` | Template for your settings / API keys. |

---

## Run it — step by step (Linux)

Open a terminal **in this folder** (`minute-man-backend`) and run these one at a time.

**1. Create an isolated Python environment** (keeps this project's packages separate):

```
python3 -m venv venv
source venv/bin/activate
```

Your prompt should now start with `(venv)`.

**2. Install the packages:**

```
pip install -r requirements.txt
```

**3. Create your settings file** from the template:

```
cp .env.example .env
```

Leave `DEFAULT_PROVIDER=demo` for now — no key needed to test.

**4. Start the server:**

```
uvicorn main:app --reload --port 8080
```

You should see `Uvicorn running on http://127.0.0.1:8080`. Leave this running.

**5. Test it.** Open a **second** terminal in the same folder and run:

```
curl -X POST http://127.0.0.1:8080/api/minutes \
  -H "Content-Type: application/json" \
  -d '{"transcript":"the yard is mud soup, slippery as, chuck crushed metal on the worst bit near the gate","meeting_type":"Toolbox Talk","site_name":"Yard","attendance":[{"name":"Shane Adams","signature":"SA"}]}'
```

You'll get back JSON with `hazards`, `actions`, `decisions`, and your `attendance`.
That same JSON can be posted to `/export/pdf` or `/export/excel` to download the files.

---

## Switching to real AI (Claude or OpenAI)

1. Get a key:
   - **Claude:** https://console.anthropic.com
   - **OpenAI:** https://platform.openai.com
2. Open `.env` and paste the key next to `ANTHROPIC_API_KEY=` or `OPENAI_API_KEY=`.
3. Change `DEFAULT_PROVIDER` to `anthropic` or `openai`.
4. Restart the server (`Ctrl+C`, then the `uvicorn` command again).

You can also choose per request by adding `"provider":"anthropic"` (or `"openai"`
/ `"demo"`) to the JSON you send to `/api/minutes`.

Check which keys the server can see any time:

```
curl http://127.0.0.1:8080/api/health
```

---

## Interactive API docs

With the server running, open **http://127.0.0.1:8080/docs** in a browser — FastAPI
gives you a clickable page to try every endpoint without curl.

---

## Next step (not done yet)

The prototype front-end still builds its export in the browser. To connect it to
this backend, point its "Generate" button at `POST /api/minutes` and its export
buttons at `/export/pdf` and `/export/excel`, using these exact field names
(`control_tier`, `compliance_note`, `by_when`, `meeting_type`, `site_name`,
`meeting_date`). See `../minute-man-backend-FIXES.md` section B2.
