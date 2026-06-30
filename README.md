# AI Debate Arena

I built this project to run a structured multi room debate across fixed role tabs while keeping the working state on disk between runs.

The goal is simple. I want a repeatable way to test an idea from several angles, keep the conversation disciplined, and resume the work later without rebuilding the setup from scratch.

## What it does

- Runs a local control panel with Flask
- Saves each run as its own session
- Uses separate role rooms for operator, investor, customer, and moderator
- Connects to a Chromium based browser over remote debugging
- Writes session state, transcript, ledger, and final output to local files
- Lets me pause, resume, and stop safely without losing the current decision trail

## Why I built it this way

I did not want a one shot script that throws text into a couple of tabs and hopes for the best.

I wanted three things

- Stable session files that survive restarts
- A UI that makes session editing obvious before a resume
- A runtime that can guard against blank rooms, stale state, and accidental overlap between runs

That is why the app separates the control panel from the runner.

`app.py` owns session management, form validation, and launch flow.

`arena.py` owns browser routing, turn execution, moderation flow, state persistence, and safe stop behavior.

## Main flow

1. Start the local control panel
2. Create a session or open an existing one
3. Paste the exact conversation link for each role room
4. Save the prompts and runtime settings
5. Launch a new run or resume an existing one
6. Let the runner move turn by turn while writing local state
7. Stop safely when needed and continue later from the saved session

## Local setup

Tested on Python 3 with Flask and Playwright.

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Start the app

Run the control panel

```
python3 app.py
```

Then start Microsoft Edge with remote debugging enabled

```
open -na "Microsoft Edge" --args --remote-debugging-port=9222
```

After that, open the local control panel in the browser on port `5050`.

## Session model

Every session gets its own folder under `runtime/sessions`.

That folder holds the full local working state for the run, including

- `config.json`
- `arena_state.json`
- `decision_ledger.md`
- `transcript.md`
- `final_result.md`
- `transport_state.json`
- `launch.log`

This matters because resume is not a vague retry. Resume reads the saved session files and continues from the current state.

## UI behavior

The control panel is built around one rule.

Save first, then run.

When I change prompts, role links, turn limits, or retry settings, those values are written into the selected session before the runner starts. That removes the common failure mode where the UI shows one thing and the runner uses something older from disk.

The UI also rejects generic home page links. Each active role must point to the exact conversation room that should receive the next turn.

## Runtime behavior

The runner supports two modes.

- Two role relay
- Moderated relay with three or four active roles

In moderated runs, the moderator decides who speaks next and what decision the next turn should resolve.

The runner also keeps a session lock so the same session cannot be launched twice at the same time.

If I request a safe stop, the runner preserves the state and exits at a clean checkpoint instead of cutting the session in the middle of a turn.

## Project structure

`app.py`  
Local Flask control panel for session setup, save, resume, and status views

`arena.py`  
Browser driven runner with turn routing, moderation flow, persistence, and recovery logic

`runtime/`  
Local session data and state files

`transcripts/`  
Legacy transcript output for non session runs

`edge-arena-profile/`  
Local browser profile used by the runner

## Practical notes

- The browser profile, runtime files, and transcripts stay local
- The repository only needs source code and documentation
- If a session is already active, the launcher will refuse a second run for the same session
- If a role room points to a generic landing page, the run will stop before sending turns into the wrong place

## Good fit

This project is a good fit if you want a local research loop that feels controlled, inspectable, and restart friendly.

It is not built as a hosted product. It is a local operating tool that favors traceability and state recovery over convenience shortcuts.

## Closing note

If you are reading this later, the mindset behind the project is straightforward.

Make the run state visible.

Keep the launch flow explicit.

Do not hide the files that actually decide what happens next.
