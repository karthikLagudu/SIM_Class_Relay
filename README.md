# Class Relay

**Class Relay by Super Intelli Machines** is a classroom workspace for coordinating assignments, submissions, and feedback across the web and Telegram.

[Quick start](#quick-start) · [Demo walkthrough](DEMO.md) · [Telegram setup](TELEGRAM_SETUP.md) · [Architecture](ARCHITECTURE.md)

## Workspaces

| Role | What you can do |
| --- | --- |
| Coordinator | Manage schools, classes, teaching teams, and enrollments. Review submissions across a school. **Remove from school** revokes the student's school access while preserving work history for authorized staff and memberships elsewhere. |
| Teacher | Draft assignments from text or dictation, review instructions and recipients before publishing, adjust deadlines, track progress, reply to help requests, and review submissions. |
| Student | See assigned work, report progress, ask for help, submit text or files, and review feedback and revision requests. |

Open work and review pages check for changes every three seconds and preserve unsaved edits. The sun and moon toggle switches between light and dark mode, with light as the default.

## Quick start

Install Python 3.12 or newer and run the commands from the repository root. Use the quick seed for a small walkthrough or the [full demo setup](DEMO.md#full-reviewer-dataset) for three schools, nine classes, twelve teachers, and 75 students.

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if (!(Test-Path .env)) { Copy-Item .env.example .env }
$env:DEMO_MODE = 'true'
$env:LLM_MODE = 'fake'
$env:SPEECH_MODE = 'off'
$env:TELEGRAM_MODE = 'log'
$env:TELEGRAM_UPDATES = 'webhook'
.\.venv\Scripts\python.exe -m scripts.seed
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8767 --no-access-log
```

**macOS and Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
[ -e .env ] || cp .env.example .env
export DEMO_MODE=true LLM_MODE=fake SPEECH_MODE=off TELEGRAM_MODE=log TELEGRAM_UPDATES=webhook
.venv/bin/python -m scripts.seed
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8767 --no-access-log
```

Open [Class Relay](http://127.0.0.1:8767/login). Select **Show demo accounts**, choose **Coordinators**, **Teachers**, or **Students**, and select **Use this account**. Sign in to open the workspace. Demo sign-in and live Telegram use this same local address.

### Try a complete workflow

1. Sign in as the demo teacher, open Grade 8 - A, create a draft, and publish it for the class.
2. Switch to a demo student, open the assignment, and submit an answer or attach a file.
3. Return to the teacher's **Review queue**. Request a revision or mark the submission complete, then open the student's assignment to see the feedback.

For staffing changes, school-wide removal, access checks, and the scripted acceptance test, see the [demo walkthrough](DEMO.md).

## Live Telegram

Use `TELEGRAM_MODE=real` with `TELEGRAM_UPDATES=polling` for a local bot. Hosted HTTPS deployments can use webhooks. Follow the [Telegram setup guide](TELEGRAM_SETUP.md) to configure BotFather credentials and switch transports safely. Keep the token in your local `.env` or deployment secrets.

Teachers and coordinators can use **Connect my Telegram**. Students receive a private connection link from authorized class staff. Once connected, send `/menu` in the bot to see the actions available to your role. Linked students receive assignment and feedback notifications. Linked teachers and coordinators receive scoped alerts for submissions and help requests.

OpenAI is optional for Telegram. Set `LLM_MODE=real` for model-assisted interpretation or `SPEECH_MODE=openai` for teacher dictation. Both require an OpenAI API key.

## Configuration

| Setting | Default and purpose |
| --- | --- |
| `ENVIRONMENT`, `DEMO_MODE` | `development` and `false`. Enable the account picker on a seeded development database with the fake LLM. In live Telegram mode the picker is available on loopback only. |
| `DATABASE_URL` | `sqlite:///./classroom_companion.db`. Startup creates missing tables and applies additive upgrades. |
| `SESSION_SECRET` | Signs sessions and CSRF tokens. Set a long random value for shared deployments. |
| `BASE_URL` | `http://127.0.0.1:8767`. Use a public HTTPS origin for webhook registration. |
| `TELEGRAM_MODE`, `TELEGRAM_UPDATES` | `log` and `webhook`. Log mode simulates delivery. Real mode uses Bot API delivery with polling or a webhook for incoming updates. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME` | BotFather credentials for live delivery and connection links. |
| `TELEGRAM_WEBHOOK_SECRET` | Set a long random value for real webhook mode. The app checks the path and matching secret header. |
| `TELEGRAM_LINK_TOKEN_MINUTES` | `30`. Lifetime of a one-time connection link. |
| `LLM_MODE`, `OPENAI_MODEL` | `fake` and `gpt-5-mini`. Real mode requires `OPENAI_API_KEY` and a model that supports Structured Outputs. |
| `SPEECH_MODE`, `OPENAI_TRANSCRIPTION_MODEL` | `off` and `gpt-transcribe`. Set `SPEECH_MODE=openai` with `OPENAI_API_KEY` to enable dictation. |
| `LLM_CONFIDENCE_THRESHOLD` | `0.75`. Low-confidence requests ask for clarification. |
| `SCHOOL_TIMEZONE` | `Asia/Kolkata`, the default IANA timezone for new schools. |
| `QUIET_HOUR_START`, `QUIET_HOUR_END` | School-local `22` and `7`. Equal values disable quiet hours. |
| `REMINDER_INTERVAL_SECONDS` | `45`. Set to `0` to pause reminder evaluation while queued live Telegram deliveries continue. |
| `UPLOAD_DIR`, `MAX_UPLOAD_BYTES` | `uploads` and `10485760` bytes (10 MiB). Downloads require workspace authorization. |
| `CONVERSATION_CONTEXT_MINUTES`, `NOTIFICATION_MAX_ATTEMPTS` | `30` minutes and `3` delivery attempts. |

## Architecture

FastAPI and Jinja serve the role-based workspaces. The web app and Telegram share authorization and assignment services backed by SQLite. Assignment changes and pending notification records commit together, then a worker sends eligible messages and records the result. OpenAI interprets requests when enabled. Application code checks permissions, deadlines, and state transitions.

See [Architecture](ARCHITECTURE.md) for data and delivery flows and [Product reasoning](REASONING.md) for access rules, reminders, and retry behavior.

## Verification and operations

Run these commands with the virtual environment's Python.

```bash
python -m ruff check app scripts tests
python -m pytest -q
```

[GitHub Actions](https://github.com/karthikLagudu/SIM_Class_Relay/actions/workflows/test.yml) tests Python 3.12, 3.13, and 3.14. Tests mock external providers. Use the [offline acceptance script](DEMO.md#quick-offline-acceptance-script) to exercise the running application.

Run one FastAPI process for the current SQLite setup. For a shared HTTPS deployment, set `ENVIRONMENT=production` and use a long random session secret. The [deployment notes](ARCHITECTURE.md#deployment-shape) explain the storage and worker changes needed to scale beyond one process.

The `site/` directory contains the static project overview. To publish it, configure GitHub Pages to use Actions and set `PUBLISH_PROJECT_SITE=true`. The interactive application runs separately on FastAPI.
