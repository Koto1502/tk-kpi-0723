const fs = require("fs");

const html = fs.readFileSync("churn.html", "utf8");
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/gi)]
  .map((match) => match[1]);

if (!scripts.length) throw new Error("No script blocks found");
scripts.forEach((script) => new Function(script));

for (const text of [
  "Campaign",
  "Install date from",
  "Install date to",
  "Download CSV",
  "User churn by playtime",
  "Tutorial step exits",
  "Users left by campaign level",
  "Last custom event before player quit",
]) {
  if (!html.includes(text)) throw new Error(`Missing required UI text: ${text}`);
}

if (/"(?:af_id|appsflyer_id|user_id|user_pseudo_id)"\s*:/.test(html)) {
  throw new Error("A raw identifier field leaked into the published payload");
}

console.log(
  `Validated ${scripts.length} script block(s), ${html.length.toLocaleString()} bytes`
);
