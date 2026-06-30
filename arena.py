#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, sync_playwright

from role_config import ROLE_LABELS, active_roles_for_mode


CDP_URL = "http://127.0.0.1:9222"
CDP_RETRY_SECONDS = 5
ROOM_GOTO_TIMEOUT_MS = 25_000
ROOM_RETRY_INITIAL_SECONDS = 5
ROOM_RETRY_MAX_SECONDS = 60
POLL_INTERVAL_SECONDS = 3
RESPONSE_STABLE_SECONDS = 12
RESPONSE_TIMEOUT_SECONDS = 600
MIN_RESPONSE_CHARS = 120
MAX_PROMPT_CHARS = 18_000
MAX_LEDGER_CHARS = 18_000
MAX_PEER_CHARS = 8_500
MAX_TASK_CHARS = 9_000
MAX_STATE_RESPONSE_CHARS = 16_000

PHASES = {
    "constraint_lock",
    "candidate_generation",
    "candidate_elimination",
    "top_three_comparison",
    "business_line_selection",
    "offer_design",
    "validation_design",
    "final_verdict",
}

PHASE_ALIASES = {
    "top_three": "top_three_comparison",
    "top_three_adversarial_relay": "top_three_comparison",
    "test_leader_selection": "business_line_selection",
}

TURN_TYPES = {
    "opening_position",
    "rebuttal",
    "cross_examination",
    "evidence_test",
    "stress_test",
    "candidate_comparison",
    "offer_design",
    "validation_design",
    "decision_review",
    "synthesis",
    "assumption_resolution",
    "candidate_generation",
    "candidate_elimination",
}


class ArenaAlreadyRunning(RuntimeError):
    pass


class RoleRoomRoutingError(RuntimeError):
    pass


class RecoverableTransportError(RuntimeError):
    pass


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()

    if len(text) <= limit:
        return text

    left = limit // 2

    return (
        text[:left]
        + "\n\n...[middle omitted from live prompt; full text remains in local files]...\n\n"
        + text[-(limit - left):]
    )


def normalize_token(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9_]+",
        "_",
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_"),
    ).strip("_")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(path.suffix + ".tmp")

    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    temporary.replace(path)


def parse_field(text: str, field: str) -> str | None:
    source = str(text or "").replace("**", "").replace("`", "")

    match = re.search(
        rf"(?im)^\s*{re.escape(field)}\s*:\s*(.+?)\s*$",
        source,
    )

    return match.group(1).strip() if match else None


def parse_block(text: str, start: str, end: str) -> str | None:
    match = re.search(
        re.escape(start) + r"\s*(.*?)\s*" + re.escape(end),
        str(text or ""),
        re.IGNORECASE | re.DOTALL,
    )

    if not match:
        return None

    result = match.group(1).strip()

    return None if not result or result.upper() == "N/A" else result


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


def active_room_roles(mode: str) -> list[str]:
    return active_roles_for_mode(mode)


def url_host(url: str) -> str:
    host = urlparse(url).netloc.lower()

    return host[4:] if host.startswith("www.") else host


def url_path(url: str) -> str:
    path = urlparse(url).path.rstrip("/")

    return path or "/"


def is_generic_chatgpt_root(url: str) -> bool:
    return url_host(url) == "chatgpt.com" and url_path(url) == "/"


def validate_room_url(role: str, raw_url: Any) -> str:
    url = str(raw_url or "").strip()
    parsed = urlparse(url)

    if (
        not url
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
    ):
        raise RoleRoomRoutingError(
            f"{ROLE_LABELS[role]} URL is invalid: {url!r}"
        )

    if is_generic_chatgpt_root(url):
        raise RoleRoomRoutingError(
            f"{ROLE_LABELS[role]} URL is generic https://chatgpt.com/. "
            "Save that role's exact conversation URL in the UI."
        )

    return url


def same_room(expected_url: str, actual_url: str) -> bool:
    return (
        not is_generic_chatgpt_root(actual_url)
        and url_host(expected_url) == url_host(actual_url)
        and url_path(expected_url) == url_path(actual_url)
    )


class Paths:
    def __init__(self, session_id: str | None):
        base = Path(__file__).resolve().parent
        runtime = base / "runtime"

        if session_id:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", session_id):
                raise ValueError("Invalid session id")

            root = runtime / "sessions" / session_id
        else:
            root = runtime

        self.base = base
        self.root = root
        self.session_id = session_id
        self.config = root / "config.json"
        self.state = root / "arena_state.json"
        self.transcript = (
            root / "transcript.md"
            if session_id
            else base / "transcripts" / "latest.md"
        )
        self.ledger = root / "decision_ledger.md"
        self.final = root / "final_result.md"
        self.pending = root / "pending_turn.json"
        self.transport = root / "transport_state.json"
        self.transport_log = root / "transport_recovery.log"
        self.stop = root / "stop.txt"
        self.lock = root / "arena.lock"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.transcript.parent.mkdir(parents=True, exist_ok=True)


class SessionLock:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.held = False

    def acquire(self) -> None:
        self.paths.ensure()

        payload = {
            "pid": os.getpid(),
            "session_id": self.paths.session_id or "legacy",
            "started_at": now(),
        }

        for _ in range(2):
            try:
                descriptor = os.open(
                    self.paths.lock,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )

                with os.fdopen(
                    descriptor,
                    "w",
                    encoding="utf-8",
                ) as file:
                    json.dump(
                        payload,
                        file,
                        ensure_ascii=False,
                        indent=2,
                    )

                self.held = True
                return

            except FileExistsError:
                old = read_json(self.paths.lock, {})

                try:
                    old_pid = (
                        int(old.get("pid", 0))
                        if isinstance(old, dict)
                        else 0
                    )
                except Exception:
                    old_pid = 0

                if pid_is_alive(old_pid):
                    raise ArenaAlreadyRunning(
                        f"This session is already controlled by PID {old_pid}."
                    )

                self.paths.lock.unlink(missing_ok=True)

        raise ArenaAlreadyRunning("Could not acquire arena.lock")

    def release(self) -> None:
        if not self.held:
            return

        old = read_json(self.paths.lock, {})

        if (
            isinstance(old, dict)
            and old.get("pid") == os.getpid()
        ):
            self.paths.lock.unlink(missing_ok=True)

        self.held = False


class Arena:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.paths.ensure()

        self.config = self.load_config()
        self.mode = self.config["arena_mode"]

        self.roles = [
            role
            for role in active_room_roles(self.mode)
            if role != "moderator"
        ]

        self.has_moderator = self.mode in {
            "three_ai",
            "four_ai",
        }

        self.pages: dict[str, Page] = {}
        self.lock = SessionLock(paths)
        self.state = self.load_state()
        self.run_turns_completed = 0

    def load_config(self) -> dict[str, Any]:
        config = read_json(self.paths.config, {})

        if not isinstance(config, dict) or not config:
            raise RuntimeError(
                f"Missing or invalid session config: {self.paths.config}"
            )

        mode = str(config.get("arena_mode", "")).strip()

        if mode not in {
            "two_ai",
            "three_ai",
            "four_ai",
        }:
            raise RuntimeError(f"Unknown arena_mode: {mode!r}")

        config["arena_mode"] = mode
        config["prompt"] = (
            config.get("prompt")
            or config.get("business_prompt")
            or config.get("business_context")
            or ""
        )

        config.setdefault("run_style", "fixed")
        config.setdefault("fixed_turns", 18)
        config.setdefault("safety_cap_turns", 60)
        config.setdefault("transport_retry_initial_seconds", 8)
        config.setdefault("transport_retry_max_seconds", 120)

        for role in active_room_roles(mode):
            config[f"{role}_url"] = validate_room_url(
                role,
                config.get(f"{role}_url"),
            )

        return config

    def default_state(self) -> dict[str, Any]:
        decision = {
            "id": "D-00",
            "question": (
                "Which role should provide the first highest-information "
                "contribution, and what exact decision should it resolve?"
            ),
            "status": "open",
        }

        return {
            "schema_version": 24,
            "created_at": now(),
            "updated_at": now(),
            "phase": "constraint_lock",
            "phase_status": "active",
            "decision_id": decision["id"],
            "decision_required": decision["question"],
            "current_decision": decision,
            "current_leader": "None",
            "ledger": (
                "FINAL TARGET:\n"
                "Select one specific business line, one specific productized offer, "
                "and one validation plan the team can execute.\n\n"
                "CURRENT PHASE:\nconstraint_lock\n\n"
                "ACTIVE CANDIDATES:\n- None yet.\n\n"
                "ELIMINATED CANDIDATES:\n- None yet.\n\n"
                "CURRENT LEADER:\n- None yet.\n\n"
                "OPEN QUESTION:\n"
                "- Moderator must choose the one role with the highest information "
                "value for the first turn.\n"
            ),
            "latest_by_role": {
                role: ""
                for role in self.roles
            },
            "relay_turn_count": 0,
            "history": [],
            "inflight_relay": None,
        }

    def load_state(self) -> dict[str, Any]:
        state = read_json(self.paths.state, {})

        if not isinstance(state, dict) or not state:
            state = self.default_state()
            self.save_state(state)
            return state

        defaults = self.default_state()

        for key, value in defaults.items():
            state.setdefault(key, value)

        phase = PHASE_ALIASES.get(
            normalize_token(state.get("phase")),
            normalize_token(state.get("phase")),
        )

        state["phase"] = (
            phase
            if phase in PHASES
            else "constraint_lock"
        )

        if not isinstance(
            state.get("latest_by_role"),
            dict,
        ):
            state["latest_by_role"] = {}

        for role in self.roles:
            state["latest_by_role"].setdefault(role, "")

        if not isinstance(
            state.get("current_decision"),
            dict,
        ):
            state["current_decision"] = {
                "id": state.get("decision_id", "D-00"),
                "question": state.get(
                    "decision_required",
                    "",
                ),
                "status": "open",
            }

        return state

    def save_state(
        self,
        state: dict[str, Any] | None = None,
    ) -> None:
        state = state or self.state
        state["updated_at"] = now()

        disk = {
            **state,
            "latest_by_role": {
                role: clip(text, MAX_STATE_RESPONSE_CHARS)
                for role, text in state[
                    "latest_by_role"
                ].items()
            },
            "ledger": clip(
                state["ledger"],
                MAX_LEDGER_CHARS,
            ),
        }

        write_json(self.paths.state, disk)

        self.paths.ledger.write_text(
            "# AI Arena Decision Ledger\n\n"
            f"Updated: {now()}\n\n"
            f"Phase: {state['phase']}\n\n"
            f"Decision ID: {state['decision_id']}\n\n"
            f"Decision required: {state['decision_required']}\n\n"
            f"{state['ledger']}",
            encoding="utf-8",
        )

    def save_turn(
        self,
        speaker: str,
        text: str,
    ) -> None:
        if not self.paths.transcript.exists():
            self.paths.transcript.write_text(
                "# AI Arena Transcript\n"
                f"\nStarted: {now()}\n"
                f"\nMode: {self.mode}\n",
                encoding="utf-8",
            )

        with self.paths.transcript.open(
            "a",
            encoding="utf-8",
        ) as file:
            file.write(
                f"\n\n## {speaker}\n\n{text}\n"
            )

    def stop_requested(self) -> bool:
        return self.paths.stop.exists()

    def sleep(self, seconds: float) -> None:
        deadline = time.time() + seconds

        while time.time() < deadline:
            if self.stop_requested():
                raise KeyboardInterrupt(
                    "Stop requested via stop.txt"
                )

            time.sleep(
                min(
                    1,
                    max(
                        0.05,
                        deadline - time.time(),
                    ),
                )
            )

    def log_transport(
        self,
        role: str,
        event: str,
        message: str,
        **extra: Any,
    ) -> None:
        row = {
            "at": now(),
            "role": role,
            "event": event,
            "message": message,
            **extra,
        }

        with self.paths.transport_log.open(
            "a",
            encoding="utf-8",
        ) as file:
            file.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                )
                + "\n"
            )

    def set_transport(
        self,
        role: str,
        status: str,
        message: str,
        **extra: Any,
    ) -> None:
        data = read_json(self.paths.transport, {})

        if not isinstance(data, dict):
            data = {}

        data[role] = {
            "at": now(),
            "status": status,
            "message": message,
            **extra,
        }

        write_json(self.paths.transport, data)

    def pending(self) -> dict[str, Any] | None:
        data = read_json(self.paths.pending, {})

        return (
            data
            if isinstance(data, dict) and data
            else None
        )

    def save_pending(
        self,
        pending: dict[str, Any],
    ) -> None:
        pending["updated_at"] = now()
        write_json(self.paths.pending, pending)

    def clear_pending(self) -> None:
        self.paths.pending.unlink(missing_ok=True)

    def connect_context(self, playwright) -> BrowserContext:
        while True:
            if self.stop_requested():
                raise KeyboardInterrupt(
                    "Stop requested via stop.txt"
                )

            try:
                browser = playwright.chromium.connect_over_cdp(
                    CDP_URL
                )

                if browser.contexts:
                    print("Connected to Edge CDP.")
                    return browser.contexts[0]

                browser.close()

            except Exception as error:
                print(
                    f"Waiting for Edge CDP at {CDP_URL}: "
                    f"{type(error).__name__}. "
                    f"Retrying in {CDP_RETRY_SECONDS}s."
                )

            self.sleep(CDP_RETRY_SECONDS)

    def open_role_room(
        self,
        context: BrowserContext,
        role: str,
    ) -> Page:
        expected_url = self.config[f"{role}_url"]
        page = context.new_page()
        attempt = 0

        print(
            f"Opening {ROLE_LABELS[role]} configured room: "
            f"{expected_url}"
        )

        while True:
            if self.stop_requested():
                page.close()
                raise KeyboardInterrupt(
                    "Stop requested via stop.txt"
                )

            attempt += 1
            warning = ""

            try:
                page.goto(
                    expected_url,
                    wait_until="commit",
                    timeout=ROOM_GOTO_TIMEOUT_MS,
                )
            except Exception as error:
                warning = f"{type(error).__name__}: {error}"

            self.sleep(2)
            actual_url = page.url

            if same_room(expected_url, actual_url):
                page.bring_to_front()
                self.sleep(1)

                print(
                    f"Verified {ROLE_LABELS[role]} room: "
                    f"{actual_url}"
                )

                self.log_transport(
                    role,
                    "room_verified",
                    "Configured role room opened and verified.",
                    expected_url=expected_url,
                    actual_url=actual_url,
                    navigation_attempt=attempt,
                    navigation_warning=warning,
                )

                return page

            if is_generic_chatgpt_root(actual_url):
                page.close()

                raise RoleRoomRoutingError(
                    f"{ROLE_LABELS[role]} was redirected to generic ChatGPT home.\n"
                    f"Expected: {expected_url}\n"
                    f"Actual:   {actual_url}"
                )

            delay = min(
                ROOM_RETRY_MAX_SECONDS,
                ROOM_RETRY_INITIAL_SECONDS
                * (2 ** min(attempt - 1, 4)),
            )

            message = (
                "Role room has not reached its configured conversation yet. "
                f"Actual URL: {actual_url or 'about:blank'}. "
                f"{warning or 'Waiting for navigation.'} "
                f"Retrying in {delay}s."
            )

            print(
                f"[transport] {ROLE_LABELS[role]}: {message}"
            )

            self.log_transport(
                role,
                "room_navigation_retry",
                message,
                expected_url=expected_url,
                actual_url=actual_url,
                navigation_attempt=attempt,
                retry_in_seconds=delay,
            )

            self.set_transport(
                role,
                "opening_room_retry",
                message,
                expected_url=expected_url,
                actual_url=actual_url,
                retry_in_seconds=delay,
            )

            self.sleep(delay)

    def open_role_pages(
        self,
        context: BrowserContext,
    ) -> None:
        print("\nConfigured session role rooms:")

        for role in active_room_roles(self.mode):
            print(
                f"- {ROLE_LABELS[role]}: "
                f"{self.config[f'{role}_url']}"
            )

            self.pages[role] = self.open_role_room(
                context,
                role,
            )

    def collect_texts(
        self,
        page: Page,
        selector: str,
    ) -> list[str]:
        texts: list[str] = []

        try:
            locator = page.locator(selector)

            for index in range(locator.count()):
                try:
                    text = locator.nth(index).inner_text(
                        timeout=1500
                    ).strip()

                    if text:
                        texts.append(text)
                except Exception:
                    pass
        except Exception:
            pass

        return texts

    def get_assistant_messages(
        self,
        page: Page,
    ) -> list[str]:
        primary_messages = self.collect_texts(
            page,
            '[data-message-author-role="assistant"]',
        )

        if primary_messages:
            return primary_messages

        for selector in [
            "article",
            '[class*="markdown"]',
            '[class*="prose"]',
        ]:
            fallback_messages = self.collect_texts(page, selector)

            if fallback_messages:
                return fallback_messages

        return []

    def is_generating(self, page: Page) -> bool:
        selectors = [
            '[data-testid="stop-button"]',
            'button[aria-label*="Stop"]',
            'button[aria-label*="stop"]',
            'button:has-text("Stop generating")',
            '[aria-label*="Stop generating"]',
            '[aria-label*="stop generating"]',
        ]

        for selector in selectors:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                pass

        return False

    def send_prompt(
        self,
        page: Page,
        prompt: str,
        role_name: str,
    ) -> list[str]:
        page.bring_to_front()
        time.sleep(1)

        baseline_messages = self.get_assistant_messages(page)

        selectors = [
            "#prompt-textarea",
            "div[contenteditable='true']",
            "textarea",
            "[role='textbox']",
        ]

        for selector in selectors:
            try:
                box = page.locator(selector).last
                box.wait_for(timeout=10_000)
                box.click()

                page.keyboard.press("Meta+A")
                page.keyboard.press("Backspace")
                page.keyboard.insert_text(prompt)

                time.sleep(0.8)
                page.keyboard.press("Enter")

                print(
                    f"Sent prompt to {role_name}. "
                    f"Assistant-message baseline: "
                    f"{len(baseline_messages)}"
                )

                return baseline_messages
            except Exception:
                pass

        raise RuntimeError(
            f"Could not find a usable input box "
            f"for {role_name}."
        )

    def read_new_stable_response(
        self,
        page: Page,
        role_name: str,
        baseline_messages: list[str],
        timeout_seconds: int = RESPONSE_TIMEOUT_SECONDS,
        stable_seconds: int = RESPONSE_STABLE_SECONDS,
    ) -> str:
        page.bring_to_front()

        baseline_count = len(baseline_messages)
        baseline_last = (
            baseline_messages[-1]
            if baseline_messages
            else ""
        )

        print(
            f"Waiting for new {role_name} response. "
            f"Baseline assistant messages: {baseline_count}"
        )

        started_at = time.time()
        current_response = ""
        last_change_at = time.time()
        saw_response = False

        while time.time() - started_at < timeout_seconds:
            if self.stop_requested():
                raise KeyboardInterrupt(
                    "Stop requested via stop.txt"
                )

            messages = self.get_assistant_messages(page)
            candidate = ""

            if len(messages) > baseline_count:
                candidate = messages[-1]
            elif (
                messages
                and messages[-1] != baseline_last
            ):
                candidate = messages[-1]

            if candidate:
                saw_response = True

                if candidate != current_response:
                    current_response = candidate
                    last_change_at = time.time()

                stable_for = int(
                    time.time() - last_change_at
                )
                generating = self.is_generating(page)

                print(
                    f"{role_name}: messages={len(messages)} "
                    f"baseline={baseline_count} "
                    f"current={len(current_response)} "
                    f"stable={stable_for}s "
                    f"generating={generating}"
                )

                if (
                    len(current_response)
                    >= MIN_RESPONSE_CHARS
                    and stable_for >= stable_seconds
                    and not generating
                ):
                    print(
                        f"Captured new {role_name} response."
                    )
                    return current_response

            time.sleep(POLL_INTERVAL_SECONDS)

        if (
            saw_response
            and len(current_response)
            >= MIN_RESPONSE_CHARS
        ):
            print(
                f"Captured {role_name} response by timeout."
            )
            return current_response

        raise RecoverableTransportError(
            f"Timed out waiting for a new response "
            f"from {role_name}."
        )

    def online(self, page: Page) -> bool:
        try:
            return bool(
                page.evaluate(
                    "() => navigator.onLine"
                )
            )
        except Exception:
            return True

    def wait_for_connection(
        self,
        page: Page,
        role: str,
        stage: str,
    ) -> None:
        initial = max(
            1,
            int(
                self.config[
                    "transport_retry_initial_seconds"
                ]
            ),
        )

        maximum = max(
            initial,
            int(
                self.config[
                    "transport_retry_max_seconds"
                ]
            ),
        )

        attempt = 0

        while not self.online(page):
            attempt += 1

            delay = min(
                maximum,
                initial * (2 ** min(attempt - 1, 5)),
            )

            message = (
                f"Offline during {stage}; preserving the same "
                f"turn and retrying in {delay}s."
            )

            print(
                f"[transport] {ROLE_LABELS[role]}: {message}"
            )

            self.log_transport(
                role,
                "offline_wait",
                message,
                retry_in_seconds=delay,
            )

            self.set_transport(
                role,
                "offline_wait",
                message,
                retry_in_seconds=delay,
            )

            self.sleep(delay)

    def run_agent(
        self,
        role: str,
        speaker: str,
        prompt: str,
        turns: list[tuple[str, str]],
    ) -> str:
        prompt_hash = hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()

        pending = self.pending()

        resumable = {
            "prepared",
            "sending_unknown",
            "awaiting_response",
            "recovering",
        }

        if not (
            isinstance(pending, dict)
            and pending.get("role") == role
            and pending.get("prompt_hash") == prompt_hash
            and pending.get("status") in resumable
        ):
            pending = {
                "turn_id": hashlib.sha256(
                    f"{role}|{speaker}|{time.time_ns()}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:24],
                "role": role,
                "speaker": speaker,
                "prompt": prompt,
                "prompt_hash": prompt_hash,
                "status": "prepared",
                "created_at": now(),
                "send_attempts": 0,
                "response_attempts": 0,
                "baseline_messages": [],
            }

            self.save_pending(pending)
        else:
            prompt = str(
                pending.get("prompt", prompt)
            )
            speaker = str(
                pending.get("speaker", speaker)
            )

        page = self.pages[role]

        initial = max(
            1,
            int(
                self.config[
                    "transport_retry_initial_seconds"
                ]
            ),
        )

        maximum = max(
            initial,
            int(
                self.config[
                    "transport_retry_max_seconds"
                ]
            ),
        )

        while True:
            try:
                if pending["status"] == "prepared":
                    pending["baseline_messages"] = (
                        self.get_assistant_messages(page)
                    )
                    pending["status"] = "sending_unknown"
                    self.save_pending(pending)

                    self.wait_for_connection(
                        page,
                        role,
                        "prompt delivery",
                    )

                    baseline = self.send_prompt(
                        page,
                        prompt,
                        speaker,
                    )

                    pending["baseline_messages"] = baseline
                    pending["status"] = "awaiting_response"
                    pending["send_attempts"] += 1
                    pending["sent_at"] = now()

                    self.save_pending(pending)

                    self.log_transport(
                        role,
                        "prompt_sent",
                        "Original send_prompt() completed.",
                        turn_id=pending["turn_id"],
                    )

                elif pending["status"] == "sending_unknown":
                    pending["status"] = "awaiting_response"
                    self.save_pending(pending)

                baseline = pending.get(
                    "baseline_messages",
                    [],
                )

                if not isinstance(baseline, list):
                    baseline = []

                self.wait_for_connection(
                    page,
                    role,
                    "response reading",
                )

                response = self.read_new_stable_response(
                    page,
                    speaker,
                    baseline,
                )

                self.save_turn(speaker, response)
                turns.append((speaker, response))
                self.clear_pending()

                self.set_transport(
                    role,
                    "response_captured",
                    "Stable response captured by original detector.",
                    response_chars=len(response),
                )

                return response

            except KeyboardInterrupt:
                pending["status"] = "recovering"
                self.save_pending(pending)
                raise

            except Exception as error:
                pending["response_attempts"] += 1

                if pending["status"] == "sending_unknown":
                    pending["status"] = "awaiting_response"

                self.save_pending(pending)

                delay = min(
                    maximum,
                    initial
                    * (
                        2
                        ** min(
                            pending["response_attempts"] - 1,
                            5,
                        )
                    ),
                )

                message = (
                    f"{type(error).__name__}: {error}. Preserving the same "
                    f"prompt and baseline; retrying in {delay}s without choosing "
                    "another role."
                )

                print(
                    f"[transport] {speaker}: {message}"
                )

                self.log_transport(
                    role,
                    "transport_retry",
                    message,
                    retry_in_seconds=delay,
                    turn_id=pending["turn_id"],
                )

                self.set_transport(
                    role,
                    "recovering",
                    message,
                    retry_in_seconds=delay,
                )

                self.sleep(delay)

    def peer_positions(
        self,
        exclude: str | None = None,
    ) -> str:
        blocks: list[str] = []

        for role in self.roles:
            if role == exclude:
                continue

            response = self.state[
                "latest_by_role"
            ].get(role, "").strip()

            if response:
                blocks.append(
                    f"{ROLE_LABELS[role].upper()} LATEST POSITION:\n"
                    f"{clip(response, MAX_PEER_CHARS)}"
                )

        return (
            "\n\n".join(blocks)
            or "No participant position exists yet."
        )

    def participant_prompt(
        self,
        role: str,
        task: str,
        meta: dict[str, Any] | None,
    ) -> str:
        peer_context = (
            "No additional Moderator-selected peer context."
        )

        if (
            isinstance(meta, dict)
            and meta.get("peer_context")
        ):
            peer_context = str(meta["peer_context"])

        return f"""You are {ROLE_LABELS[role]}.

Assigned role:

{self.config.get(f'{role}_role', '')}

Think privately before answering. Do not reveal hidden chain-of-thought.
Output structured conclusions, assumptions, evidence, tradeoffs, and recommendations.

Business plan / original problem:

{clip(self.config['prompt'], MAX_PROMPT_CHARS)}

CURRENT DECISION STATE:
Phase: {self.state['phase']}
Decision ID: {self.state['decision_id']}
Decision required: {self.state['decision_required']}
Current leader: {self.state['current_leader']}

LIVE DECISION LEDGER:
{clip(self.state['ledger'], MAX_LEDGER_CHARS)}

MODERATOR-SELECTED PEER CONTEXT:
{clip(peer_context, MAX_PEER_CHARS)}

LATEST PEER POSITIONS:
{self.peer_positions(exclude=role)}

TASK ASSIGNED BY MODERATOR:
{clip(task, MAX_TASK_CHARS)}

Rules:
- Answer the exact decision assigned by Moderator.
- Address named peer claims and contradictions, not generic frameworks.
- Distinguish facts, reasonable inferences, provisional assumptions, and required validation.
- Do not choose the next speaker. Moderator owns turn selection.
- Do not force agreement. Honest dissent with a resolution path is useful.

End with these exact fields:
POSITION: support / reject / revise / uncertain
DECISION_IMPACT: one concise sentence
ASSUMPTION_STATUS: supported / provisional_usable / decision_blocking / low_impact_defer
EVIDENCE_NEEDED: concrete tests, artifacts, calculations, or buyer actions
NEW_INFORMATION: yes or no
"""

    def moderator_prompt(self) -> str:
        role_names = " / ".join(self.roles)
        turn_types = " / ".join(sorted(TURN_TYPES))
        phases = " / ".join(sorted(PHASES))

        return f"""You are the Moderator and research director of a persistent AI Debate Arena.

Assigned role:

{self.config.get('moderator_role', '')}

Think privately before answering. Do not reveal hidden chain-of-thought.

You control the substantive relay. This sequence is mandatory:
Moderator -> exactly one selected participant -> Moderator -> exactly one selected participant.
There is no automatic Role 1 -> Role 2 -> Role 3 order. Do not choose roles by fairness, rotation, or turn order. Choose the single role whose next contribution has the highest expected information value for the current exact decision.

At the beginning there may be no participant positions. You must still choose the best first role and assign one bounded, high-information task.

Business plan / original problem:

{clip(self.config['prompt'], MAX_PROMPT_CHARS)}

CURRENT DECISION STATE:
Phase: {self.state['phase']}
Decision ID: {self.state['decision_id']}
Decision required: {self.state['decision_required']}
Current leader: {self.state['current_leader']}
Participant turns completed: {self.state['relay_turn_count']}

LIVE DECISION LEDGER:
{clip(self.state['ledger'], MAX_LEDGER_CHARS)}

LATEST PARTICIPANT POSITIONS:
{self.peer_positions()}

Your job:
- Update the ledger with evidence, candidates, assumptions, contradictions, dissent, and current leader.
- Identify the highest-value unresolved question.
- Choose exactly one participant to answer next.
- Give that participant a precise task with an advancement rule and a kill or downgrade rule.
- Continue until finalization is genuinely justified. Missing field validation is not a reason to stop.

Your response must start with exactly these fields:
ACTION: CONTINUE or FINALIZE
PHASE: {phases}
DECISION_ID: D-XX
DECISION_REQUIRED: one exact decision sentence
NEXT_SPEAKER: {role_names} or N/A
TURN_TYPE: {turn_types}
REASON: why this one role has the highest information value now
ADVANCEMENT_RULE: what result permits the next decision
KILL_OR_DOWNGRADE_RULE: what result rejects or downgrades the candidate or assumption

Then provide exactly these blocks:

PEER_CONTEXT_START
Quote or accurately paraphrase only the claims the chosen participant must address. For the first turn, state which starting uncertainty they must resolve.
PEER_CONTEXT_END

TASK_FOR_NEXT_SPEAKER_START
One precise bounded task. It must answer the current decision and state what evidence, calculation, comparison, or test is required.
TASK_FOR_NEXT_SPEAKER_END

LEDGER_UPDATE_START
Write the full updated living ledger. Preserve concrete details. Include CURRENT LEADER explicitly.
LEDGER_UPDATE_END

Rules:
- If ACTION is CONTINUE, NEXT_SPEAKER must be exactly one active participant and both blocks must be non-empty.
- If ACTION is FINALIZE, NEXT_SPEAKER must be N/A and both blocks must be N/A.
- Never ask all roles to answer in one turn.
- Never permit a participant to select the next speaker.
- Do not finalize merely because evidence is incomplete; create a bounded validation task unless the ledger truly supports a final research decision.
"""

    def parse_moderator(
        self,
        response: str,
    ) -> dict[str, Any]:
        action = (
            parse_field(response, "ACTION")
            or ""
        ).upper()

        phase = PHASE_ALIASES.get(
            normalize_token(parse_field(response, "PHASE")),
            normalize_token(parse_field(response, "PHASE")),
        )

        return {
            "action": action,
            "phase": phase,
            "decision_id": parse_field(
                response,
                "DECISION_ID",
            ),
            "decision_required": parse_field(
                response,
                "DECISION_REQUIRED",
            ),
            "next_speaker": normalize_token(
                parse_field(response, "NEXT_SPEAKER")
            ),
            "turn_type": normalize_token(
                parse_field(response, "TURN_TYPE")
            ),
            "reason": parse_field(response, "REASON"),
            "advancement_rule": parse_field(
                response,
                "ADVANCEMENT_RULE",
            ),
            "kill_rule": parse_field(
                response,
                "KILL_OR_DOWNGRADE_RULE",
            ),
            "peer_context": parse_block(
                response,
                "PEER_CONTEXT_START",
                "PEER_CONTEXT_END",
            ),
            "task": parse_block(
                response,
                "TASK_FOR_NEXT_SPEAKER_START",
                "TASK_FOR_NEXT_SPEAKER_END",
            ),
            "ledger": parse_block(
                response,
                "LEDGER_UPDATE_START",
                "LEDGER_UPDATE_END",
            ),
        }

    def moderator_valid(
        self,
        meta: dict[str, Any],
    ) -> bool:
        if meta["action"] not in {
            "CONTINUE",
            "FINALIZE",
        }:
            return False

        if (
            meta["phase"] not in PHASES
            or not meta["decision_id"]
            or not meta["decision_required"]
            or not meta["ledger"]
        ):
            return False

        if meta["action"] == "FINALIZE":
            return meta["next_speaker"] in {
                "",
                "na",
                "n_a",
            }

        return (
            meta["next_speaker"] in self.roles
            and meta["turn_type"] in TURN_TYPES
            and bool(
                meta["reason"]
                and meta["advancement_rule"]
                and meta["kill_rule"]
                and meta["peer_context"]
                and meta["task"]
            )
        )

    def apply_moderator(
        self,
        meta: dict[str, Any],
    ) -> None:
        self.state["phase"] = meta["phase"]
        self.state["decision_id"] = meta["decision_id"]
        self.state["decision_required"] = (
            meta["decision_required"]
        )

        self.state["current_decision"] = {
            "id": meta["decision_id"],
            "question": meta["decision_required"],
            "status": "open",
        }

        self.state["ledger"] = meta["ledger"]

        leader = re.search(
            r"(?im)^\s*CURRENT LEADER\s*:\s*(.+?)\s*$",
            meta["ledger"],
        )

        if leader:
            self.state["current_leader"] = (
                leader.group(1).strip()
            )

        self.state["history"].append(
            {
                "at": now(),
                "source": "moderator",
                "phase": meta["phase"],
                "decision_id": meta["decision_id"],
                "next_speaker": meta["next_speaker"],
                "reason": meta["reason"],
            }
        )

        self.state["history"] = self.state["history"][-100:]
        self.save_state()

    def request_moderator(
        self,
        turns: list[tuple[str, str]],
    ) -> dict[str, Any]:
        response = self.run_agent(
            "moderator",
            "Moderator",
            self.moderator_prompt(),
            turns,
        )

        meta = self.parse_moderator(response)

        if self.moderator_valid(meta):
            self.apply_moderator(meta)
            return meta

        repair_prompt = f"""You are Moderator Format Repair.

Your prior answer was invalid for the arena relay. Preserve the decision substance, but output a corrected moderator decision in the exact required fields and blocks. Do not choose participants by rotation.

Previous answer:
{clip(response, 12_000)}

{self.moderator_prompt()}
"""

        repaired = self.run_agent(
            "moderator",
            "Moderator Format Repair",
            repair_prompt,
            turns,
        )

        meta = self.parse_moderator(repaired)

        if not self.moderator_valid(meta):
            raise RuntimeError(
                "Moderator response remained invalid after one repair. "
                "Stopped safely instead of choosing a participant in code."
            )

        self.apply_moderator(meta)
        return meta

    def run_participant(
        self,
        role: str,
        task: str,
        turns: list[tuple[str, str]],
        meta: dict[str, Any] | None,
        prompt_override: str | None = None,
    ) -> str:
        prompt = (
            prompt_override
            or self.participant_prompt(
                role,
                task,
                meta,
            )
        )

        response = self.run_agent(
            role,
            ROLE_LABELS[role],
            prompt,
            turns,
        )

        self.state["latest_by_role"][role] = response
        self.save_state()

        return response

    def inflight_is_moderator_selected(
        self,
        inflight: dict[str, Any],
    ) -> bool:
        if inflight.get("selected_by") == "moderator":
            return True

        meta = inflight.get("meta")

        return (
            isinstance(meta, dict)
            and normalize_token(
                meta.get("next_speaker")
            )
            == inflight.get("role")
            and inflight.get("role") in self.roles
        )

    def resume_moderator_selected_turn(
        self,
        turns: list[tuple[str, str]],
    ) -> None:
        inflight = self.state.get("inflight_relay")

        if not isinstance(inflight, dict) or not inflight:
            return

        role = inflight.get("role")

        if (
            role not in self.roles
            or not self.inflight_is_moderator_selected(
                inflight
            )
        ):
            self.save_turn(
                "SYSTEM",
                "Discarded legacy non-moderator inflight relay. "
                "Moderator will choose the next substantive turn.",
            )

            self.state["inflight_relay"] = None
            self.save_state()
            return

        pending = self.pending()
        prompt_override = None

        if (
            isinstance(pending, dict)
            and pending.get("role") == role
        ):
            prompt_override = (
                str(
                    pending.get("prompt", "")
                ).strip()
                or None
            )

        print(
            "\n===== RESUMING MODERATOR-SELECTED TURN: "
            f"{ROLE_LABELS[role].upper()} ====="
        )

        self.run_participant(
            role,
            str(inflight.get("task", "")),
            turns,
            inflight.get("meta"),
            prompt_override,
        )

        self.state["relay_turn_count"] = (
            int(
                self.state.get(
                    "relay_turn_count",
                    0,
                )
            )
            + 1
        )

        self.run_turns_completed += 1
        self.state["inflight_relay"] = None
        self.save_state()

    def stop_due(self) -> tuple[bool, str]:
        if self.stop_requested():
            return True, "manual stop requested"

        limit = int(
            self.config["fixed_turns"]
            if self.config["run_style"] == "fixed"
            else self.config["safety_cap_turns"]
        )

        return (
            self.run_turns_completed >= limit,
            f"configured run checkpoint reached after {limit} new participant turns",
        )

    def final_prompt(self, reason: str) -> str:
        return f"""You are Moderator Final.

Reason for finalization: {reason}

Business plan:
{clip(self.config['prompt'], MAX_PROMPT_CHARS)}

Final living ledger:
{clip(self.state['ledger'], MAX_LEDGER_CHARS)}

Participant positions:
{self.peer_positions()}

Write the final research synthesis. Include selected business direction, bounded first offer, buyer, price hypothesis, evidence basis, unresolved uncertainty, validation steps, pass criteria, kill criteria, alternatives rejected, and dissent. Do not claim commercial validation without actual field or payment evidence.
"""

    def finalize(
        self,
        turns: list[tuple[str, str]],
        reason: str,
    ) -> None:
        final = self.run_agent(
            "moderator",
            "Moderator Final",
            self.final_prompt(reason),
            turns,
        )

        self.paths.final.write_text(
            final,
            encoding="utf-8",
        )

        self.state["phase"] = "final_verdict"
        self.state["phase_status"] = "finalized"
        self.state["final_result"] = final
        self.save_state()

    def run_moderated(
        self,
        turns: list[tuple[str, str]],
    ) -> None:
        self.resume_moderator_selected_turn(turns)

        while True:
            stop, reason = self.stop_due()

            if stop:
                message = (
                    f"Moderated relay checkpoint: {reason}. "
                    f"This run completed {self.run_turns_completed} new participant turns. "
                    "State is saved; Resume starts a new checkpoint and Moderator chooses the next role."
                )

                print(f"\n{message}")
                self.save_turn("SYSTEM", message)
                self.save_state()
                return

            print(
                "\n===== MODERATOR DECISION BEFORE PARTICIPANT TURN "
                f"{int(self.state.get('relay_turn_count', 0)) + 1} ====="
            )

            meta = self.request_moderator(turns)

            print(
                "Moderator decision: "
                f"phase={meta['phase']}, "
                f"decision={meta['decision_id']}, "
                f"next={meta['next_speaker']}, "
                f"type={meta['turn_type']}"
            )

            if meta["action"] == "FINALIZE":
                self.finalize(
                    turns,
                    meta["reason"]
                    or "Moderator finalized the decision ledger.",
                )
                return

            selected_role = meta["next_speaker"]

            self.state["inflight_relay"] = {
                "selected_by": "moderator",
                "selected_at": now(),
                "role": selected_role,
                "task": meta["task"],
                "meta": meta,
            }

            self.save_state()

            print(
                "\n===== MODERATOR SELECTED "
                f"{ROLE_LABELS[selected_role].upper()} "
                f"FOR TURN "
                f"{int(self.state.get('relay_turn_count', 0)) + 1} ====="
            )

            self.run_participant(
                selected_role,
                meta["task"],
                turns,
                meta,
            )

            self.state["relay_turn_count"] = (
                int(
                    self.state.get(
                        "relay_turn_count",
                        0,
                    )
                )
                + 1
            )

            self.run_turns_completed += 1
            self.state["inflight_relay"] = None
            self.save_state()

    def run_two_ai(
        self,
        turns: list[tuple[str, str]],
    ) -> None:
        next_role = "operator"

        while True:
            stop, reason = self.stop_due()

            if stop:
                message = (
                    f"Two-role relay checkpoint: {reason}. "
                    f"This run completed {self.run_turns_completed} new participant turns."
                )

                print(f"\n{message}")
                self.save_turn("SYSTEM", message)
                self.save_state()
                return

            other = (
                "investor"
                if next_role == "operator"
                else "operator"
            )

            task = f"""Respond to the latest {ROLE_LABELS[other]} position.

{clip(self.state['latest_by_role'].get(other, ''), MAX_PEER_CHARS)}

Challenge one material claim, add evidence or a testable assumption, and update the business direction only where justified."""

            meta = {
                "peer_context": (
                    f"Latest speaker: {ROLE_LABELS[other]}"
                )
            }

            self.state["inflight_relay"] = {
                "selected_by": "two_ai_rotation",
                "role": next_role,
                "task": task,
                "meta": meta,
            }

            self.save_state()

            self.run_participant(
                next_role,
                task,
                turns,
                meta,
            )

            self.state["relay_turn_count"] = (
                int(
                    self.state.get(
                        "relay_turn_count",
                        0,
                    )
                )
                + 1
            )

            self.run_turns_completed += 1
            self.state["inflight_relay"] = None
            self.save_state()

            next_role = other

    def run(self) -> None:
        self.lock.acquire()
        atexit.register(self.lock.release)

        self.paths.stop.unlink(missing_ok=True)
        self.save_state()

        print(
            f"\nLoaded session config: {self.paths.config}"
        )

        print(
            "\nConfigured rooms from the current saved session UI:"
        )

        for role in active_room_roles(self.mode):
            print(
                f"- {ROLE_LABELS[role]}: "
                f"{self.config[f'{role}_url']}"
            )

        try:
            with sync_playwright() as playwright:
                context = self.connect_context(playwright)
                self.open_role_pages(context)

                print(
                    "\nAll configured rooms are verified. "
                    "Starting the arena now."
                )

                turns: list[tuple[str, str]] = []

                if self.has_moderator:
                    print(
                        "Moderator-led mode: Moderator selects the first role "
                        "and every later role."
                    )

                    self.run_moderated(turns)
                else:
                    self.run_two_ai(turns)

        except KeyboardInterrupt as error:
            self.save_turn(
                "SYSTEM",
                f"Stopped manually: {error}",
            )

            self.save_state()

            print(
                f"Stopped. State preserved in {self.paths.root}"
            )

        except Exception as error:
            self.save_turn(
                "SYSTEM",
                f"ARENA ERROR: {type(error).__name__}: {error}",
            )

            self.save_state()

            print(
                f"\nArena stopped: "
                f"{type(error).__name__}: {error}"
            )

            raise

        finally:
            self.lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run AI Debate Arena"
    )

    parser.add_argument(
        "--session-id",
        default=None,
    )

    args = parser.parse_args()

    Arena(Paths(args.session_id)).run()


if __name__ == "__main__":
    main()
