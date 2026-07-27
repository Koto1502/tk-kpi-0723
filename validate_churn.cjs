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
  "Inactive players by playtime",
  "Tutorial step exits",
  "Players left in game at each campaign level",
  "Quest reward funnel",
  "Last custom event before player became inactive",
]) {
  if (!html.includes(text)) throw new Error(`Missing required UI text: ${text}`);
}

if (/"(?:af_id|appsflyer_id|user_id|user_pseudo_id)"\s*:/.test(html)) {
  throw new Error("A raw identifier field leaked into the published payload");
}

const payloadStart = html.indexOf("const DATA = ") + "const DATA = ".length;
const payloadEnd = html.indexOf(";\nconst $", payloadStart);
const data = JSON.parse(html.slice(payloadStart, payloadEnd));
if (data.meta.inactivity_hours !== 72) throw new Error("Expected 72-hour inactivity rule");
if (data.players.some((row) => "churned" in row || "online_time" in row)) {
  throw new Error("Legacy app-removal fields remain in the payload");
}
if (data.players.some((row) =>
  !["quest_any", "quest_main", "quest_daily", "quest_diary"].every((key) => key in row)
)) {
  throw new Error("Quest funnel flags are missing from the payload");
}
const inactive = data.players.filter((row) => row.inactive);
const zeroPlaytime = inactive.filter((row) => row.playtime_seconds === 0);

console.log(
  `Validated ${scripts.length} script block(s), ${data.players.length.toLocaleString()} players, ` +
  `${inactive.length.toLocaleString()} inactive, ${zeroPlaytime.length.toLocaleString()} zero-playtime`
);
