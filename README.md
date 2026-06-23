# Tacit

Tacit is a knowledge-to-content platform for LinkedIn. This Sprint 1 prototype supports:

- Account signup and login.
- Workspace creation.
- Voice profile setup.
- PDF/DOCX/TXT upload and pasted source text.
- Insight extraction.
- LinkedIn draft generation.
- Draft editing, approval, rejection, and scheduling.
- Calendar view.

## Run Locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

If `OPENAI_API_KEY` is not set, Tacit uses a deterministic local fallback so the workflow can still be tested.
