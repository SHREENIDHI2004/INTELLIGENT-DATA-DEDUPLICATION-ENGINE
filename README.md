# ModApp — Data Profiler Pro UI + Data Steward

Django app that combines:

1. **Data Profiler Pro–style UI** (look and feel: cards, metrics, primary color `#2563eb`, layout).
2. **Two deduplication features** from the Data Profiler Pro deduplication app (same logic):
   - **Exact Match** — find and remove rows identical on selected columns.
   - **Fuzzy Match** — find similar text (typos, names) with RapidFuzz, Jaro-Winkler, Metaphone, etc.; merge groups (keep first).
3. **Data Steward features** from data_steward_ui:
   - Upload Excel (UID, Item_Name, Location, Category, Fuzzy_Deduplication_Candidates, etc.).
   - Records list with filters (All / Pending / Reviewed) and search.
   - Review & Merge: side-by-side comparison, select best value per field, Merge / Merge Separately / Mark Reviewed.
   - Optional **Azure OpenAI** recommendations in Review (set `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`).
   - Export merged master list to Excel.

## Setup

```bash
cd modapp
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/

## Workflow

- **Load Data** — Upload CSV/Excel for in-app duplicate detection (used by Find Duplicates).
- **Find Duplicates** — Exact and Fuzzy tabs; find groups, remove exact duplicates or merge fuzzy groups (same logic as Data Profiler Pro).
- **Upload Master** — Import Excel into Records (steward workflow).
- **Records List** — Filter pending duplicates, open Review & Merge.
- **Review & Merge** — Compare main vs candidates, optional AI recommendation, merge and export.

## Optional: Azure OpenAI

Set in environment or `modapp/settings.py`:

- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_VERSION` (default `2025-01-01-preview`)
- `AZURE_OPENAI_DEPLOYMENT` (default `gpt-4o`)

AI is used only on the Review & Merge page when you click “Ask AI”.
