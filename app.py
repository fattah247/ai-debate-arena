#!/usr/bin/env python3
"""Local Flask control panel for Persistent AI Debate Arena.

The selected session is editable from the UI.

Save Changes:
    writes the current form values into:
    runtime/sessions/<session-id>/config.json

Save Changes & Resume:
    writes the current form values first, then launches:
    python arena.py --session-id <session-id>

arena.py reads that saved config.json at startup.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template_string,
    request,
    url_for,
)


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / "runtime"
SESSIONS_DIR = RUNTIME_DIR / "sessions"
ARENA_PATH = BASE_DIR / "arena.py"
LAST_SESSION_PATH = RUNTIME_DIR / "last_session.json"

HOST = "127.0.0.1"
PORT = 5050

SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
DEFAULT_CHAT_URL = "https://chatgpt.com/"

app = Flask(__name__)
app.config.update(
    SECRET_KEY="local-ai-debate-arena-control-panel",
    JSON_SORT_KEYS=False,
)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_directories() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(path.suffix + ".tmp")

    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary.replace(path)


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def valid_session_id(value: str | None) -> bool:
    return bool(
        SESSION_ID_RE.fullmatch(
            str(value or "").strip()
        )
    )


def require_session_id(value: str | None) -> str:
    session_id = str(value or "").strip()

    if not valid_session_id(session_id):
        abort(404)

    return session_id


def session_root(session_id: str) -> Path:
    return SESSIONS_DIR / require_session_id(session_id)


def session_config_path(session_id: str) -> Path:
    return session_root(session_id) / "config.json"


def bounded_int(
    value: Any,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(
            str(
                value
                if value is not None
                else default
            ).strip()
        )
    except (TypeError, ValueError):
        parsed = default

    return max(minimum, min(maximum, parsed))


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def is_generic_chatgpt_root(url: str) -> bool:
    parsed = urlparse(
        str(url or "").strip()
    )

    host = parsed.netloc.lower()

    if host.startswith("www."):
        host = host[4:]

    path = parsed.path.rstrip("/") or "/"

    return host == "chatgpt.com" and path == "/"


def validate_role_url(
    role_label: str,
    value: str | None,
) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
    ):
        raise ValueError(
            f"{role_label} URL must be a complete "
            "http:// or https:// URL."
        )

    if is_generic_chatgpt_root(url):
        raise ValueError(
            f"{role_label} URL cannot be the generic "
            "https://chatgpt.com/ home page. "
            "Paste that role's exact conversation URL."
        )

    return url


# ---------------------------------------------------------------------------
# Session config
# ---------------------------------------------------------------------------


def defaults() -> dict[str, Any]:
    return {
        "arena_mode": "four_ai",
        "run_style": "fixed",
        "fixed_turns": 18,
        "safety_cap_turns": 60,
        "stall_review_limit": 3,
        "transport_retry_initial_seconds": 8,
        "transport_retry_max_seconds": 120,
        "operator_url": DEFAULT_CHAT_URL,
        "investor_url": DEFAULT_CHAT_URL,
        "customer_url": DEFAULT_CHAT_URL,
        "moderator_url": DEFAULT_CHAT_URL,
        "shared_prompt": "",
        "business_prompt": "",
        "prompt": "",
        "operator_role": "",
        "investor_role": "",
        "customer_role": "",
        "moderator_role": "",
    }


def normalize_config(raw: Any) -> dict[str, Any]:
    values = defaults()

    if not isinstance(raw, dict):
        return values

    for key in values:
        if key in raw and raw[key] is not None:
            values[key] = raw[key]

    business_prompt = str(
        raw.get("business_prompt")
        or raw.get("business_context")
        or raw.get("prompt")
        or ""
    )

    values["business_prompt"] = business_prompt
    values["prompt"] = business_prompt

    return values


def load_session_config(
    session_id: str,
) -> dict[str, Any]:
    return normalize_config(
        read_json(
            session_config_path(session_id),
            {},
        )
    )


def active_roles_for_mode(
    arena_mode: str,
) -> list[str]:
    roles = ["operator", "investor"]

    if arena_mode == "four_ai":
        roles.append("customer")

    if arena_mode in {"three_ai", "four_ai"}:
        roles.append("moderator")

    return roles


def form_config() -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []

    arena_mode = str(
        request.form.get("arena_mode", "four_ai")
    ).strip()

    run_style = str(
        request.form.get("run_style", "fixed")
    ).strip()

    if arena_mode not in {
        "two_ai",
        "three_ai",
        "four_ai",
    }:
        errors.append("Arena mode is invalid.")

    if run_style not in {
        "fixed",
        "infinite",
    }:
        errors.append("Run style is invalid.")

    role_labels = {
        "operator": "Operator",
        "investor": "Investor + Market Analyst",
        "customer": "Customer",
        "moderator": "Moderator",
    }

    active_roles = active_roles_for_mode(
        arena_mode
    )

    urls: dict[str, str] = {}

    for role, label in role_labels.items():
        raw_url = str(
            request.form.get(
                f"{role}_url",
                "",
            )
        ).strip()

        if role not in active_roles and not raw_url:
            urls[f"{role}_url"] = DEFAULT_CHAT_URL
            continue

        try:
            urls[f"{role}_url"] = validate_role_url(
                label,
                raw_url,
            )
        except ValueError as exc:
            if role in active_roles:
                errors.append(str(exc))
            else:
                urls[f"{role}_url"] = (
                    raw_url
                    or DEFAULT_CHAT_URL
                )

    shared_prompt = str(
        request.form.get("shared_prompt", "")
    ).strip()

    business_prompt = str(
        request.form.get("business_prompt", "")
    ).strip()

    roles = {
        "operator_role": str(
            request.form.get(
                "operator_role",
                "",
            )
        ).strip(),
        "investor_role": str(
            request.form.get(
                "investor_role",
                "",
            )
        ).strip(),
        "customer_role": str(
            request.form.get(
                "customer_role",
                "",
            )
        ).strip(),
        "moderator_role": str(
            request.form.get(
                "moderator_role",
                "",
            )
        ).strip(),
    }

    if not shared_prompt:
        errors.append(
            "Shared Initial Prompt cannot be empty."
        )

    if not business_prompt:
        errors.append(
            "Business Context / Original Problem "
            "cannot be empty."
        )

    if not roles["operator_role"]:
        errors.append(
            "Operator role cannot be empty."
        )

    if not roles["investor_role"]:
        errors.append(
            "Investor role cannot be empty."
        )

    if (
        arena_mode == "four_ai"
        and not roles["customer_role"]
    ):
        errors.append(
            "Customer role cannot be empty in 4 AI mode."
        )

    if (
        arena_mode in {"three_ai", "four_ai"}
        and not roles["moderator_role"]
    ):
        errors.append(
            "Moderator role cannot be empty "
            "in moderated mode."
        )

    retry_initial = bounded_int(
        request.form.get(
            "transport_retry_initial_seconds"
        ),
        8,
        1,
        300,
    )

    retry_maximum = bounded_int(
        request.form.get(
            "transport_retry_max_seconds"
        ),
        120,
        5,
        900,
    )

    if retry_maximum < retry_initial:
        errors.append(
            "Maximum retry delay must be at least "
            "the initial retry delay."
        )

    config = {
        "arena_mode": arena_mode,
        "run_style": run_style,
        "fixed_turns": bounded_int(
            request.form.get("fixed_turns"),
            18,
            1,
            250,
        ),
        "safety_cap_turns": bounded_int(
            request.form.get("safety_cap_turns"),
            60,
            1,
            1000,
        ),
        "stall_review_limit": bounded_int(
            request.form.get("stall_review_limit"),
            3,
            1,
            20,
        ),
        "transport_retry_initial_seconds": retry_initial,
        "transport_retry_max_seconds": retry_maximum,
        **urls,
        "shared_prompt": shared_prompt,
        "business_prompt": business_prompt,
        # Kept for arena.py compatibility.
        "prompt": business_prompt,
        **roles,
        "app_version": "session-ui-save-resume-v1",
    }

    return config, errors


# ---------------------------------------------------------------------------
# Session status and launcher
# ---------------------------------------------------------------------------


def make_session_id() -> str:
    return (
        datetime.now().strftime(
            "arena-%Y%m%d-%H%M%S-"
        )
        + uuid.uuid4().hex[:6]
    )


def set_last_session(
    session_id: str,
) -> None:
    write_json(
        LAST_SESSION_PATH,
        {
            "session_id": session_id,
            "updated_at": now(),
        },
    )


def selected_session() -> str | None:
    requested = str(
        request.args.get("session", "")
    ).strip()

    if (
        requested
        and valid_session_id(requested)
        and session_root(requested).exists()
    ):
        return requested

    previous = read_json(
        LAST_SESSION_PATH,
        {},
    )

    if isinstance(previous, dict):
        candidate = str(
            previous.get("session_id", "")
        ).strip()

        if (
            candidate
            and valid_session_id(candidate)
            and session_root(candidate).exists()
        ):
            return candidate

    sessions = list_sessions()

    return sessions[0]["id"] if sessions else None


def lock_status(root: Path) -> dict[str, Any]:
    lock = read_json(
        root / "arena.lock",
        {},
    )

    if not isinstance(lock, dict):
        lock = {}

    try:
        pid = int(lock.get("pid", 0))
    except Exception:
        pid = 0

    return {
        "exists": (root / "arena.lock").exists(),
        "pid": pid or None,
        "alive": pid_is_alive(pid),
        "started_at": lock.get("started_at", ""),
    }


def session_status(
    session_id: str,
) -> dict[str, Any]:
    root = session_root(session_id)

    state = read_json(
        root / "arena_state.json",
        {},
    )

    pending = read_json(
        root / "pending_turn.json",
        {},
    )

    transport = read_json(
        root / "transport_state.json",
        {},
    )

    if not isinstance(state, dict):
        state = {}

    if not isinstance(pending, dict):
        pending = {}

    if not isinstance(transport, dict):
        transport = {}

    lock = lock_status(root)

    if lock["alive"]:
        display_status = "runner_active"
    elif pending:
        display_status = "paused_with_pending_turn"
    elif (root / "final_result.md").exists():
        display_status = "finalized"
    else:
        display_status = "saved"

    current_decision = state.get(
        "current_decision"
    )

    if not isinstance(current_decision, dict):
        current_decision = {
            "id": state.get("decision_id", ""),
            "question": state.get(
                "decision_required",
                "",
            ),
        }

    return {
        "id": session_id,
        "display_status": display_status,
        "phase": state.get(
            "phase",
            "not_started",
        ),
        "relay_turn_count": state.get(
            "relay_turn_count",
            0,
        ),
        "current_decision": current_decision,
        "pending_turn": pending,
        "transport": transport,
        "lock": lock,
        "updated_at": state.get(
            "updated_at",
            "",
        ),
    }


def list_sessions() -> list[dict[str, Any]]:
    ensure_directories()

    result: list[dict[str, Any]] = []

    for root in SESSIONS_DIR.iterdir():
        if (
            not root.is_dir()
            or not valid_session_id(root.name)
        ):
            continue

        if not (root / "config.json").exists():
            continue

        result.append(session_status(root.name))

    result.sort(
        key=lambda item: item.get(
            "updated_at",
            "",
        ),
        reverse=True,
    )

    return result


def command_for(
    session_id: str,
) -> str:
    return (
        f"cd {shlex.quote(str(BASE_DIR))} && "
        f"{shlex.quote(sys.executable)} "
        f"{shlex.quote(str(ARENA_PATH))} "
        f"--session-id {shlex.quote(session_id)}"
    )


def launch(
    session_id: str,
    kind: str,
) -> None:
    root = session_root(session_id)
    lock = lock_status(root)

    if lock["alive"]:
        raise RuntimeError(
            "This session is already running under "
            f"PID {lock['pid']}."
        )

    if not ARENA_PATH.exists():
        raise RuntimeError(
            f"arena.py not found at {ARENA_PATH}"
        )

    command = command_for(session_id)

    write_json(
        root / "launch_request.json",
        {
            "requested_at": now(),
            "kind": kind,
            "command": command,
        },
    )

    append_text(
        root / "launch.log",
        f"[{now()}] {kind}: {command}\n",
    )

    if platform.system() == "Darwin":
        escaped = (
            command.replace("\\", "\\\\")
            .replace('"', '\\"')
        )

        script = (
            'tell application "Terminal"\n'
            "  activate\n"
            f'  do script "{escaped}"\n'
            "end tell"
        )

        subprocess.run(
            ["osascript", "-e", script],
            check=True,
        )
        return

    subprocess.Popen(
        [
            sys.executable,
            str(ARENA_PATH),
            "--session-id",
            session_id,
        ],
        cwd=BASE_DIR,
        start_new_session=True,
    )


# ---------------------------------------------------------------------------
# Page template
# ---------------------------------------------------------------------------


PAGE = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Debate Arena</title>
  <style>
    :root {
      --bg: #0b1018;
      --panel: #121d2a;
      --input: #0a111a;
      --border: #2b4764;
      --text: #eef5fb;
      --muted: #adc0d3;
      --accent: #8ed7ff;
      --danger: #d96872;
      --ok: #77d69d;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
      line-height: 1.5;
    }

    main {
      max-width: 1480px;
      margin: 0 auto;
      padding: 28px 20px 64px;
    }

    h1 {
      margin: 0 0 6px;
      font-size: 32px;
    }

    h2 {
      margin: 0 0 12px;
      font-size: 20px;
    }

    .muted,
    .hint {
      color: var(--muted);
    }

    .hint {
      font-size: 13px;
    }

    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 365px;
      gap: 20px;
      align-items: start;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 18px;
      margin-bottom: 18px;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }

    .grid.four {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }

    label {
      display: block;
      margin-bottom: 6px;
      font-size: 14px;
      font-weight: 800;
    }

    input,
    select,
    textarea {
      width: 100%;
      background: var(--input);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      font: inherit;
    }

    textarea {
      min-height: 170px;
      resize: vertical;
    }

    textarea.big {
      min-height: 310px;
    }

    textarea.role {
      min-height: 225px;
    }

    button {
      border: 0;
      border-radius: 10px;
      padding: 11px 14px;
      font: inherit;
      font-weight: 800;
      cursor: pointer;
      background: var(--accent);
      color: #06101a;
    }

    button.secondary {
      background: #1d3147;
      color: var(--text);
      border: 1px solid var(--border);
    }

    button.danger {
      background: var(--danger);
      color: white;
    }

    button:disabled {
      opacity: .55;
      cursor: not-allowed;
    }

    .buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 16px;
    }

    .notice {
      border-radius: 12px;
      padding: 12px 14px;
      margin: 14px 0;
      border: 1px solid var(--border);
    }

    .notice.ok {
      border-color: #2f7952;
      color: #d9ffe8;
    }

    .notice.error {
      border-color: #91454e;
      color: #ffe0e3;
    }

    .session {
      padding: 11px;
      margin: 9px 0;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--input);
    }

    .session.active {
      border-color: #60bceb;
    }

    .status {
      display: inline-block;
      font-size: 12px;
      font-weight: 800;
      padding: 3px 8px;
      border-radius: 999px;
      background: #30465d;
    }

    .status.runner_active {
      background: #276745;
      color: #dbffe9;
    }

    .status.paused_with_pending_turn {
      background: #6b5425;
      color: #fff1d0;
    }

    code,
    .mono {
      background: var(--input);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 2px 5px;
      overflow-wrap: anywhere;
    }

    .live {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font:
        12px/1.45
        ui-monospace,
        SFMono-Regular,
        Menlo,
        monospace;
      background: var(--input);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px;
      min-height: 130px;
    }

    .kv {
      margin: 7px 0;
      font-size: 13px;
      overflow-wrap: anywhere;
    }

    a {
      color: var(--accent);
    }

    .tiny {
      font-size: 12px;
    }

    @media (max-width: 1050px) {
      .layout {
        grid-template-columns: 1fr;
      }

      .grid.four {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 680px) {
      main {
        padding: 18px 12px 40px;
      }

      .grid,
      .grid.four {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
<main>
  <h1>AI Debate Arena</h1>

  <p class="muted">
    The selected session is editable. <b>Save Changes</b> writes the
    current form into that session. <b>Save Changes &amp; Resume</b>
    writes first, then launches
    <code>arena.py --session-id …</code>.
  </p>

  {% if message %}
    <div class="notice ok">{{ message }}</div>
  {% endif %}

  {% if error %}
    <div class="notice error">{{ error }}</div>
  {% endif %}

  <div class="layout">
    <section>
      <form
        id="arena-form"
        method="post"
        action="{{ url_for('save_or_start') }}"
      >
        <input
          type="hidden"
          name="selected_session_id"
          value="{{ selected_session or '' }}"
        >

        <div class="panel">
          <h2>Session being edited</h2>

          {% if selected_session %}
            <p><code>{{ selected_session }}</code></p>

            <p class="hint">
              Every save below overwrites only this session's
              <code>config.json</code>. The runner reads these values
              the next time it starts.
            </p>
          {% else %}
            <p class="hint">
              No selected session. Use Start New Session to create one.
            </p>
          {% endif %}
        </div>

        <div class="panel">
          <h2>Arena configuration</h2>

          <div class="grid">
            <div>
              <label>Arena mode</label>

              <select name="arena_mode">
                <option
                  value="two_ai"
                  {% if values.arena_mode == 'two_ai' %}
                    selected
                  {% endif %}
                >
                  2 AI: Operator + Investor
                </option>

                <option
                  value="three_ai"
                  {% if values.arena_mode == 'three_ai' %}
                    selected
                  {% endif %}
                >
                  3 AI: Operator + Investor + Moderator
                </option>

                <option
                  value="four_ai"
                  {% if values.arena_mode == 'four_ai' %}
                    selected
                  {% endif %}
                >
                  4 AI: Operator + Investor + Customer + Moderator
                </option>
              </select>
            </div>

            <div>
              <label>Run style</label>

              <select name="run_style">
                <option
                  value="fixed"
                  {% if values.run_style == 'fixed' %}
                    selected
                  {% endif %}
                >
                  Fixed checkpoint
                </option>

                <option
                  value="infinite"
                  {% if values.run_style == 'infinite' %}
                    selected
                  {% endif %}
                >
                  Safety-cap checkpoint
                </option>
              </select>
            </div>
          </div>

          <div
            class="grid four"
            style="margin-top:14px;"
          >
            <div>
              <label>Fixed turns</label>
              <input
                name="fixed_turns"
                type="number"
                min="1"
                max="250"
                value="{{ values.fixed_turns }}"
              >
            </div>

            <div>
              <label>Safety cap</label>
              <input
                name="safety_cap_turns"
                type="number"
                min="1"
                max="1000"
                value="{{ values.safety_cap_turns }}"
              >
            </div>

            <div>
              <label>Stall review</label>
              <input
                name="stall_review_limit"
                type="number"
                min="1"
                max="20"
                value="{{ values.stall_review_limit }}"
              >
            </div>

            <div>
              <label>Initial retry seconds</label>
              <input
                name="transport_retry_initial_seconds"
                type="number"
                min="1"
                max="300"
                value="{{ values.transport_retry_initial_seconds }}"
              >
            </div>
          </div>

          <div
            style="max-width:25%;margin-top:14px;"
          >
            <label>Maximum retry seconds</label>
            <input
              name="transport_retry_max_seconds"
              type="number"
              min="5"
              max="900"
              value="{{ values.transport_retry_max_seconds }}"
            >
          </div>
        </div>

        <div class="panel">
          <h2>ChatGPT role-room URLs</h2>

          <p class="hint">
            Paste each exact ChatGPT conversation URL. Generic
            <code>https://chatgpt.com/</code> is rejected on save,
            so the runner cannot silently launch into a blank chat.
          </p>

          <div class="grid">
            <div>
              <label>Operator URL</label>
              <input
                name="operator_url"
                value="{{ values.operator_url }}"
                required
              >
            </div>

            <div>
              <label>Investor + Market Analyst URL</label>
              <input
                name="investor_url"
                value="{{ values.investor_url }}"
                required
              >
            </div>

            <div>
              <label>Customer URL</label>
              <input
                name="customer_url"
                value="{{ values.customer_url }}"
                required
              >
            </div>

            <div>
              <label>Moderator URL</label>
              <input
                name="moderator_url"
                value="{{ values.moderator_url }}"
                required
              >
            </div>
          </div>
        </div>

        <div class="panel">
          <h2>Prompts</h2>

          <label>Shared Initial Prompt</label>

          <textarea
            class="big"
            name="shared_prompt"
            required
          >{{ values.shared_prompt }}</textarea>

          <label style="margin-top:16px;">
            Business Context / Original Problem
          </label>

          <textarea
            class="big"
            name="business_prompt"
            required
          >{{ values.business_prompt }}</textarea>
        </div>

        <div class="panel">
          <h2>Role prompts</h2>

          <label>Operator</label>

          <textarea
            class="role"
            name="operator_role"
            required
          >{{ values.operator_role }}</textarea>

          <label style="margin-top:14px;">
            Investor + Market Analyst
          </label>

          <textarea
            class="role"
            name="investor_role"
            required
          >{{ values.investor_role }}</textarea>

          <label style="margin-top:14px;">
            Customer
          </label>

          <textarea
            class="role"
            name="customer_role"
          >{{ values.customer_role }}</textarea>

          <label style="margin-top:14px;">
            Moderator
          </label>

          <textarea
            class="role"
            name="moderator_role"
          >{{ values.moderator_role }}</textarea>
        </div>

        <div class="panel">
          <h2>Actions</h2>

          <p class="hint">
            Save before resume is mandatory. This is what makes changed
            URLs and prompts take effect for the selected session.
          </p>

          <div class="buttons">
            {% if selected_session %}
              <button
                class="secondary"
                type="submit"
                name="intent"
                value="save_selected"
              >
                Save Changes to Selected Session
              </button>

              <button
                type="submit"
                name="intent"
                value="save_resume"
              >
                Save Changes &amp; Resume Selected Session
              </button>
            {% endif %}

            <button
              class="secondary"
              type="submit"
              name="intent"
              value="start_new"
            >
              Start New Session From Current Form
            </button>
          </div>
        </div>
      </form>
    </section>

    <aside>
      <div class="panel">
        <h2>Selected session status</h2>

        {% if selected_status %}
          <div class="kv">
            <b>Status:</b>
            <span
              class="status {{ selected_status.display_status }}"
            >
              {{ selected_status.display_status }}
            </span>
          </div>

          <div class="kv">
            <b>Phase:</b>
            {{ selected_status.phase }}
          </div>

          <div class="kv">
            <b>Turns:</b>
            {{ selected_status.relay_turn_count }}
          </div>

          <div class="kv">
            <b>Decision:</b>
            {{ selected_status.current_decision.question }}
          </div>

          <div class="kv">
            <b>Pending role:</b>
            {{ selected_status.pending_turn.role or 'None' }}
          </div>

          <div class="kv">
            <b>Lock:</b>
            {% if selected_status.lock.alive %}
              PID {{ selected_status.lock.pid }}
            {% else %}
              not active
            {% endif %}
          </div>

          <form
            method="post"
            action="{{ url_for('stop_session', session_id=selected_session) }}"
          >
            <button
              class="danger"
              type="submit"
            >
              Request Safe Stop
            </button>
          </form>

          <p
            class="hint tiny"
            style="margin-top:12px;"
          >
            The safe-stop button does not save edited URLs or prompts.
            Use Save Changes first.
          </p>
        {% else %}
          <p class="hint">No selected session.</p>
        {% endif %}
      </div>

      <div class="panel">
        <h2>Saved sessions</h2>

        {% for session in sessions %}
          <div
            class="session {% if session.id == selected_session %}active{% endif %}"
          >
            <a
              href="{{ url_for('index', session=session.id) }}"
            >
              <code>{{ session.id }}</code>
            </a>

            <div style="margin:6px 0;">
              <span
                class="status {{ session.display_status }}"
              >
                {{ session.display_status }}
              </span>
            </div>

            <div class="hint">
              {{ session.phase }} ·
              {{ session.relay_turn_count }} turns
            </div>
          </div>
        {% else %}
          <p class="hint">No saved sessions.</p>
        {% endfor %}
      </div>

      {% if selected_session %}
        <div class="panel">
          <h2>Runner command</h2>

          <div class="live">
            {{ command_preview }}
          </div>
        </div>
      {% endif %}
    </aside>
  </div>
</main>
</body>
</html>
'''


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    ensure_directories()

    session_id = selected_session()

    values = (
        load_session_config(session_id)
        if session_id
        else defaults()
    )

    status = (
        session_status(session_id)
        if session_id
        else None
    )

    return render_template_string(
        PAGE,
        values=values,
        selected_session=session_id,
        selected_status=status,
        sessions=list_sessions(),
        command_preview=(
            command_for(session_id)
            if session_id
            else ""
        ),
        message=str(
            request.args.get("message", "")
        ).strip(),
        error=str(
            request.args.get("error", "")
        ).strip(),
    )


@app.post("/save")
def save_or_start():
    ensure_directories()

    intent = str(
        request.form.get("intent", "")
    ).strip()

    config, errors = form_config()

    selected = str(
        request.form.get(
            "selected_session_id",
            "",
        )
    ).strip()

    selected_ok = bool(
        selected
        and valid_session_id(selected)
        and session_root(selected).exists()
    )

    if errors:
        target = selected if selected_ok else ""

        return redirect(
            url_for(
                "index",
                session=target,
                error=" ".join(errors),
            )
        )

    if intent == "start_new":
        session_id = make_session_id()
        root = session_root(session_id)

        root.mkdir(
            parents=True,
            exist_ok=False,
        )

        config.update(
            {
                "session_id": session_id,
                "created_at": now(),
                "updated_at": now(),
            }
        )

        write_json(
            root / "config.json",
            config,
        )

        write_json(
            root / "session_metadata.json",
            {
                "session_id": session_id,
                "created_at": now(),
                "created_by": "Flask UI",
            },
        )

        set_last_session(session_id)

        try:
            launch(session_id, "new_session")
        except Exception as exc:
            return redirect(
                url_for(
                    "index",
                    session=session_id,
                    error=(
                        "Session created but launch failed: "
                        f"{exc}"
                    ),
                )
            )

        return redirect(
            url_for(
                "index",
                session=session_id,
                message=(
                    "New session saved from the current form "
                    "and launch requested."
                ),
            )
        )

    if intent not in {
        "save_selected",
        "save_resume",
    }:
        return redirect(
            url_for(
                "index",
                session=(
                    selected
                    if selected_ok
                    else ""
                ),
                error="Unknown form action.",
            )
        )

    if not selected_ok:
        return redirect(
            url_for(
                "index",
                error=(
                    "Choose an existing session before "
                    "saving or resuming."
                ),
            )
        )

    old = read_json(
        session_config_path(selected),
        {},
    )

    old_created_at = (
        old.get("created_at", now())
        if isinstance(old, dict)
        else now()
    )

    config.update(
        {
            "session_id": selected,
            "created_at": old_created_at,
            "updated_at": now(),
        }
    )

    # This is the crucial operation missing from the old Resume flow.
    # Every visible field in the UI becomes the new session config.
    write_json(
        session_config_path(selected),
        config,
    )

    set_last_session(selected)

    if intent == "save_selected":
        return redirect(
            url_for(
                "index",
                session=selected,
                message=(
                    "Current UI values were saved into this "
                    "session's config.json."
                ),
            )
        )

    (session_root(selected) / "stop.txt").unlink(
        missing_ok=True
    )

    try:
        launch(selected, "save_and_resume")
    except Exception as exc:
        return redirect(
            url_for(
                "index",
                session=selected,
                error=(
                    "Changes were saved, but launch failed: "
                    f"{exc}"
                ),
            )
        )

    return redirect(
        url_for(
            "index",
            session=selected,
            message=(
                "Current UI values were saved. Resume launch "
                "requested; arena.py will read these URLs and prompts."
            ),
        )
    )


@app.post("/session/<session_id>/stop")
def stop_session(session_id: str):
    session_id = require_session_id(session_id)
    root = session_root(session_id)

    if not (root / "config.json").exists():
        abort(404)

    (root / "stop.txt").write_text(
        f"Safe stop requested at {now()}\n",
        encoding="utf-8",
    )

    return redirect(
        url_for(
            "index",
            session=session_id,
            message=(
                "Safe stop requested. The runner will preserve "
                "state at its next safe check."
            ),
        )
    )


@app.get("/api/session/<session_id>/status")
def api_session_status(session_id: str):
    session_id = require_session_id(session_id)

    if not session_config_path(session_id).exists():
        abort(404)

    return jsonify(
        session_status(session_id)
    )


@app.get("/healthz")
def healthz():
    return jsonify(
        {
            "ok": True,
            "at": now(),
            "arena_exists": ARENA_PATH.exists(),
        }
    )


if __name__ == "__main__":
    ensure_directories()

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        use_reloader=False,
    )

