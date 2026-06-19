from flask import Flask, request, render_template_string
from datetime import datetime
import subprocess
import os
import json
import sys

app = Flask(__name__)

DEFAULT_GPT_ROLE = """Skeptical agricultural operator.

You have managed farms through crop failures, labor shortages, buyer disputes, contamination, oversupply, and cash-flow stress.

Bias:
Operational reality beats strategy.
"""

DEFAULT_GEMINI_ROLE = """Agricultural investor and systems strategist.

You have evaluated small agricultural businesses and understand capital constraints, scalability, distribution, and defensibility.

Bias:
Capital efficiency, cash flow, scalability, and defensibility matter more than technical elegance.
"""

HTML = """
<!doctype html>
<html>
<head>
  <title>GPT Gemini Arena</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      max-width: 980px;
      margin: 40px auto;
      padding: 0 20px;
      background: #111;
      color: #eee;
    }
    label {
      display: block;
      margin-bottom: 8px;
      font-weight: 700;
    }
    textarea {
      width: 100%;
      font-size: 15px;
      padding: 14px;
      border-radius: 12px;
      border: 1px solid #333;
      background: #181818;
      color: #eee;
      box-sizing: border-box;
      line-height: 1.5;
    }
    .role {
      height: 150px;
    }
    .prompt {
      height: 360px;
    }
    input {
      padding: 10px;
      border-radius: 8px;
      border: 1px solid #333;
      background: #181818;
      color: #eee;
    }
    button {
      padding: 12px 18px;
      border-radius: 10px;
      border: 0;
      cursor: pointer;
      font-weight: 700;
    }
    .box {
      margin-top: 22px;
    }
    .hint {
      color: #aaa;
      font-size: 14px;
      line-height: 1.5;
    }
  </style>
</head>
<body>
  <h1>GPT ↔ Gemini Arena</h1>
  <p class="hint">
    Enter roles and the initial prompt. This runs in a separate Microsoft Edge browser profile.
  </p>

  <form method="POST" action="/start">
    <div class="box">
      <label>GPT role</label>
      <textarea class="role" name="gpt_role" required>{{ default_gpt_role }}</textarea>
    </div>

    <div class="box">
      <label>Gemini role</label>
      <textarea class="role" name="gemini_role" required>{{ default_gemini_role }}</textarea>
    </div>

    <div class="box">
      <label>Initial prompt / business plan</label>
      <textarea class="prompt" name="prompt" required></textarea>
    </div>

    <div class="box">
      <label>Max turns</label>
      <input name="turns" type="number" value="10" min="1" max="50">
    </div>

    <div class="box">
      <button type="submit">Start Arena</button>
    </div>
  </form>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(
        HTML,
        default_gpt_role=DEFAULT_GPT_ROLE,
        default_gemini_role=DEFAULT_GEMINI_ROLE,
    )

@app.route("/start", methods=["POST"])
def start():
    prompt = request.form["prompt"]
    turns = int(request.form.get("turns", 10))
    gpt_role = request.form["gpt_role"]
    gemini_role = request.form["gemini_role"]

    os.makedirs("runtime", exist_ok=True)
    os.makedirs("transcripts", exist_ok=True)

    with open("runtime/config.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "prompt": prompt,
                "turns": turns,
                "gpt_role": gpt_role,
                "gemini_role": gemini_role,
                "created_at": datetime.now().isoformat(),
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    project_dir = os.path.abspath(os.getcwd())
    python_path = sys.executable

    apple_script = f'''
tell application "Terminal"
    activate
    do script "cd '{project_dir}' && '{python_path}' arena.py"
end tell
'''

    subprocess.Popen(["osascript", "-e", apple_script])

    return """
    <body style="font-family:-apple-system;background:#111;color:#eee;padding:40px;">
      <h2>Arena started.</h2>
      <p>A Terminal window should open. Follow the instruction there.</p>
      <p>Then Microsoft Edge should open.</p>
      <p>Transcript will be saved to <code>transcripts/latest.md</code>.</p>
      <a style="color:#9cf;" href="/">Back</a>
    </body>
    """

if __name__ == "__main__":
    app.run(port=5050, debug=False)