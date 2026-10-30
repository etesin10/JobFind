# Job Search Harness (portable, local-first) — Windows and macOS

Canonical spec and AI handoff: **`JOB_SEARCH_HARNESS_MASTER.md`** (read section 19 first).
User guide (Spanish): **`INSTRUCCIONES.md`** (also shipped inside every portable folder).
Guide for AI agents / local models (English): **`AGENT_GUIDE.md`** (also shipped; kept in sync by `tests/test_agent_guide.py`).

## Run the portable build (nothing to install)
| OS | Release zip | Start |
|---|---|---|
| Windows 10/11 x64 | `JobSearchManager-Windows-x64.zip` | double-click `JobSearchManager.exe` |
| macOS Apple Silicon | `JobSearchManager-macOS-arm64.zip` | double-click `Iniciar JobSearchManager.command` |
| macOS Intel | `JobSearchManager-macOS-x86_64.zip` | double-click `Iniciar JobSearchManager.command` |

> Status (v0.4.0): the Windows build is verified end-to-end. The macOS builds are **on hold and have never been
> produced or tested** (the scripts and CI workflow exist but have not run).

The browser opens `http://127.0.0.1:8765` (local only). Close the console/Terminal window to stop the app.
All data lives next to the app: `data/` (SQLite), `excel/`, `backups/`, `exports/`, `logs/`.
The data is OS-independent. An optional `jsh_home.txt` next to the app (e.g. `../JobSearchData`) lets
the Windows and macOS copies share one data folder.

Options: `--port 8800 --no-browser --home <folder>`; headless: `--run-sources` (discovery), `--sync`
(process the agent inbox), `--self-check`; agents: `--mcp [--read-only] [--agent-name X]` (MCP stdio server).

## Agents and AI (the app never calls a model itself)
- **Local REST API** while the app runs: `http://127.0.0.1:8765/api/v1` (OpenAPI docs at `/api/v1/docs`),
  bearer tokens (read / write) shown in *Configuración*; localhost only, no CORS.
- **MCP server** (Claude Desktop/Code, VS Code…): `JobSearchManager --mcp --home <folder>`; 18 tools
  (search, flat JD + keywords, decisions, candidate profile, companies/contacts, review and tailoring
  packages/submissions). Config snippets in *Configuración → API local y MCP*.
- **Web chats** (ChatGPT/Claude): export a review or tailoring package (`.md`) and paste the JSON answer back
  (protocols `jsh-review/1`, `jsh-tailoring/1`, schema-validated, user approval required).
- **SQLite inbox** `job_inbox` for any program (contract in `INSTRUCCIONES.md` §5c).

## Get the builds without installing anything (GitHub Actions)
PyInstaller cannot cross-compile, so `.github/workflows/build.yml` builds on GitHub-hosted runners:
`windows-latest`, `macos-15` (arm64) and `macos-15-intel`. Each job runs the tests, builds, runs
`packaging/smoke_test.py` (unzips to a path with spaces and accents, runs with no Python on PATH,
moves the folder, re-checks data) and uploads the zip as an artifact. Pushing a tag `v*` also creates
a GitHub Release with the three zips. Trigger it manually with *Actions → Build portable app → Run workflow*.

## Build locally
| OS | Command | Needs (build machine only) |
|---|---|---|
| Windows | `build.bat` → `dist\JobSearchManager\` + `dist\JobSearchManager-Windows-x64.zip` | Python 3.11 x64 (`py -3.11`, or `set PYTHON=...`) |
| macOS | `bash build_mac.sh` → `dist/JobSearchManager/` + `dist/JobSearchManager-macOS-<arch>.zip` | Python 3.11 (python.org or Homebrew; or `PYTHON=...`) |

A macOS build runs only on Macs with the same architecture as the build machine.

## Run from source (development)
```
Windows: py -3.11 -m venv .venv && .venv\Scripts\python -m pip install -r requirements-lock.txt
         .venv\Scripts\python -m jsh          |  .venv\Scripts\python -m pytest -q
macOS:   python3.11 -m venv .venv && .venv/bin/python -m pip install -r requirements-lock.txt
         .venv/bin/python -m jsh              |  .venv/bin/python -m pytest -q
```
`requirements-lock.txt` pins every package for both OSes (platform-only packages use markers).

## Layout
- `jsh/` application package: `rules/` (safe rule engine), `services/` (jobs, rules/profiles, suggestions,
  candidate/skills, discovery, sync (external agents), jd, crm (companies/contacts), reviews (AI protocol),
  tailoring (résumé + .docx)), `candidate/` (résumé parsing DOCX/PDF/TXT, skill vocabulary, extraction, JD
  matching), `connectors/` (Amazon, Greenhouse, Lever, Ashby), `excel/` (xlsx/csv), `ui/` (NiceGUI panels),
  `jd.py` (JD sections/keywords), `api.py` (REST), `mcp_server.py` (MCP), `privacy.py`, `prompts.py`,
  `db.py` (migrations/backup/restore), `models.py`, `modes.py`, `paths.py` (portable home, macOS
  .app/translocation handling, `jsh_home.txt`), `selfcheck.py` (`--self-check`: verifies résumé parsing, SQLite,
  JD analysis, packages, .docx, MCP and REST inside the packaged app).
- `tests/` pytest suite (backend, simulated UI, portability, API/MCP incl. a real stdio subprocess).
- `packaging/` PyInstaller spec, macOS launcher, dist rebuild scripts, smoke test.
- `.gitattributes` keeps `.sh`/`.command` files LF (CRLF breaks bash on macOS) and `.bat` files CRLF.
