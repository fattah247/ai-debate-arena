from playwright.sync_api import sync_playwright
import json
import os
import time
from datetime import datetime

CDP_URL = "http://127.0.0.1:9222"
TRANSCRIPT_PATH = "transcripts/latest.md"


def load_config():
    with open("runtime/config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def wait_for_enter(msg):
    input(f"\n{msg}\nPress Enter when done...")


def save_turn(speaker, text):
    os.makedirs("transcripts", exist_ok=True)
    with open(TRANSCRIPT_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n\n## {speaker}\n\n{text}\n")


def build_gpt_a_initial_prompt(gpt_role, gemini_role, business_plan):
    return f"""
You are GPT-A in a two-reviewer business stress-test.

Your assigned role:

{gpt_role}

The other reviewer is GPT-B.

GPT-B's assigned role:

{gemini_role}

Your task:
- Review the business plan below from your assigned role.
- Do not be motivational.
- Do not agree too easily.
- Attack assumptions.
- Identify operational risks, hidden costs, bottlenecks, and false moats.
- If the plan is weak, say exactly where.
- If the plan can be improved, propose a revised version.
- End with: "Challenge for GPT-B:" followed by the strongest question you can ask GPT-B.

Business plan:

{business_plan}
""".strip()


def build_gpt_b_reply_prompt(gemini_role, gpt_role, gpt_a_message):
    return f"""
You are GPT-B in a two-reviewer business stress-test.

Your assigned role:

{gemini_role}

The other reviewer is GPT-A.

GPT-A's assigned role:

{gpt_role}

GPT-A said:

{gpt_a_message}

Your task:
- Challenge GPT-A's analysis.
- Identify what GPT-A missed.
- Agree only where GPT-A is clearly correct.
- Disagree where GPT-A is weak, shallow, or too optimistic.
- Improve the business plan from your assigned role.
- End with: "Challenge for GPT-A:" followed by the strongest question you can ask GPT-A.
""".strip()


def build_gpt_a_reply_prompt(gpt_role, gemini_role, gpt_b_message):
    return f"""
You are GPT-A in a two-reviewer business stress-test.

Your assigned role:

{gpt_role}

The other reviewer is GPT-B.

GPT-B's assigned role:

{gemini_role}

GPT-B said:

{gpt_b_message}

Your task:
- Challenge GPT-B's analysis.
- Defend what is correct.
- Admit weak points where GPT-B is right.
- Push the discussion toward a practical, survivable roadmap.
- Identify remaining unresolved risks.
- End with: "Challenge for GPT-B:" followed by the strongest question you can ask GPT-B.
""".strip()


def get_or_create_chatgpt_page(context, index):
    chatgpt_pages = [p for p in context.pages if "chatgpt.com" in p.url]

    if len(chatgpt_pages) > index:
        page = chatgpt_pages[index]
    else:
        page = context.new_page()
        page.goto("https://chatgpt.com/")

    page.bring_to_front()
    return page


def send_prompt(page, prompt, name):
    page.bring_to_front()
    time.sleep(1)

    selectors = [
        "#prompt-textarea",
        "div[contenteditable='true']",
        "textarea",
        "[role='textbox']",
    ]

    last_error = None

    for selector in selectors:
        try:
            box = page.locator(selector).last
            box.wait_for(timeout=10_000)
            box.click()
            page.keyboard.insert_text(prompt)
            time.sleep(0.8)
            page.keyboard.press("Enter")
            print(f"Sent prompt to {name}.")
            return
        except Exception as e:
            last_error = e

    raise RuntimeError(f"Could not find input box for {name}. Last error: {last_error}")


def get_all_candidate_texts(page):
    selectors = [
        '[data-message-author-role="assistant"]',
        "article",
        '[class*="markdown"]',
        '[class*="prose"]',
    ]

    candidates = []

    for selector in selectors:
        try:
            elements = page.locator(selector)
            count = elements.count()

            for i in range(count):
                try:
                    text = elements.nth(i).inner_text(timeout=1500).strip()
                    if len(text) > 50:
                        candidates.append(text)
                except Exception:
                    pass
        except Exception:
            pass

    seen = set()
    deduped = []

    for text in candidates:
        key = text[:200]
        if key not in seen:
            seen.add(key)
            deduped.append(text)

    return deduped


def is_generating(page):
    selectors = [
        '[data-testid="stop-button"]',
        'button[aria-label*="Stop"]',
        'button[aria-label*="stop"]',
        'button:has-text("Stop")',
        'button:has-text("Cancel")',
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


def read_latest_stable_response(page, name, timeout_seconds=480, stable_seconds=35):
    page.bring_to_front()

    print(f"Waiting for {name} response...")

    start = time.time()
    last_text = ""
    best_text = ""
    last_change = time.time()

    while time.time() - start < timeout_seconds:
        texts = get_all_candidate_texts(page)

        if texts:
            current_text = texts[-1].strip()

            if len(current_text) > len(best_text):
                best_text = current_text

            if current_text != last_text:
                last_text = current_text
                last_change = time.time()

            stable_for = int(time.time() - last_change)
            generating = is_generating(page)

            print(
                f"{name}: current={len(current_text)} "
                f"best={len(best_text)} "
                f"stable={stable_for}s "
                f"generating={generating}"
            )

            if len(best_text) > 300 and stable_for >= stable_seconds and not generating:
                print(f"{name} response captured.")
                return best_text

        time.sleep(3)

    if best_text:
        print(f"{name} response captured by timeout.")
        return best_text

    raise RuntimeError(f"Could not read response from {name}.")


def main():
    config = load_config()

    business_plan = config["prompt"]
    max_turns = int(config["turns"])
    gpt_role = config["gpt_role"]
    gemini_role = config["gemini_role"]

    os.makedirs("runtime", exist_ok=True)
    os.makedirs("transcripts", exist_ok=True)

    with open(TRANSCRIPT_PATH, "w", encoding="utf-8") as f:
        f.write("# GPT-GPT Arena\n")
        f.write(f"\nStarted: {datetime.now().isoformat()}\n")
        f.write("\n## GPT-A Role\n\n")
        f.write(gpt_role)
        f.write("\n\n## GPT-B Role\n\n")
        f.write(gemini_role)
        f.write("\n\n## Initial Business Plan\n\n")
        f.write(business_plan)

    print("\nBefore starting, open Edge with:")
    print('pkill "Microsoft Edge"')
    print('open -na "Microsoft Edge" --args --remote-debugging-port=9222')
    print("\nThen log in normally to ChatGPT.")

    wait_for_enter("Continue when Edge is ready and ChatGPT is logged in.")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]

        gpt_a_page = get_or_create_chatgpt_page(context, 0)
        gpt_b_page = get_or_create_chatgpt_page(context, 1)

        gpt_a_page.goto("https://chatgpt.com/")
        gpt_b_page.goto("https://chatgpt.com/")

        print("GPT-A page opened.")
        print("GPT-B page opened.")

        wait_for_enter("Confirm both ChatGPT tabs are ready.")

        gpt_a_prompt = build_gpt_a_initial_prompt(
            gpt_role=gpt_role,
            gemini_role=gemini_role,
            business_plan=business_plan,
        )

        send_prompt(gpt_a_page, gpt_a_prompt, "GPT-A")
        current_gpt_a_message = read_latest_stable_response(gpt_a_page, "GPT-A")
        save_turn("GPT-A", current_gpt_a_message)

        for turn in range(1, max_turns + 1):
            print(f"\n===== TURN {turn} =====")

            gpt_b_prompt = build_gpt_b_reply_prompt(
                gemini_role=gemini_role,
                gpt_role=gpt_role,
                gpt_a_message=current_gpt_a_message,
            )

            send_prompt(gpt_b_page, gpt_b_prompt, "GPT-B")
            gpt_b_message = read_latest_stable_response(gpt_b_page, "GPT-B")
            save_turn("GPT-B", gpt_b_message)

            gpt_a_prompt = build_gpt_a_reply_prompt(
                gpt_role=gpt_role,
                gemini_role=gemini_role,
                gpt_b_message=gpt_b_message,
            )

            send_prompt(gpt_a_page, gpt_a_prompt, "GPT-A")
            current_gpt_a_message = read_latest_stable_response(gpt_a_page, "GPT-A")
            save_turn("GPT-A", current_gpt_a_message)

        print(f"\nDone. Transcript saved to {TRANSCRIPT_PATH}")
        browser.close()


if __name__ == "__main__":
    main()