"""Minimal Claude API call (streaming, adaptive thinking).

Credentials: ANTHROPIC_API_KEY, or an `ant auth login` profile. Run: uv run chat.py "question"
"""
import sys

import anthropic
from dotenv import load_dotenv

load_dotenv()
client = anthropic.Anthropic()

prompt = " ".join(sys.argv[1:]) or "Give me three tips for writing idempotent shell scripts."

# fallbacks="default": if Opus 5's safety classifiers decline, the API re-runs the
# request on Anthropic's recommended fallback model instead of returning a refusal.
with client.beta.messages.stream(
    model="claude-opus-5",
    max_tokens=64000,
    thinking={"type": "adaptive"},
    betas=["server-side-fallback-2026-07-01"],
    fallbacks="default",
    messages=[{"role": "user", "content": prompt}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
    message = stream.get_final_message()

print()
if message.stop_reason == "refusal":
    print(f"[refused: {message.stop_details}]", file=sys.stderr)
