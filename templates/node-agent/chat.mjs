// Minimal Claude API call (streaming, adaptive thinking).
// Credentials: ANTHROPIC_API_KEY, or an `ant auth login` profile. Run: npm run chat -- "question"
// After the first `npm install`, pin real versions in package.json (replace "latest").
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic();
const prompt = process.argv.slice(2).join(" ") || "Give me three tips for writing idempotent shell scripts.";

// fallbacks: "default" re-runs a declined request on Anthropic's recommended fallback model.
const stream = client.beta.messages.stream({
  model: "claude-opus-5",
  max_tokens: 64000,
  thinking: { type: "adaptive" },
  betas: ["server-side-fallback-2026-07-01"],
  fallbacks: "default",
  messages: [{ role: "user", content: prompt }],
});
stream.on("text", (text) => process.stdout.write(text));
const message = await stream.finalMessage();
console.log();
if (message.stop_reason === "refusal") console.error("[refused]", message.stop_details);
