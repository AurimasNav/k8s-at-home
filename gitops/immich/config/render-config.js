// Renders Immich's config file at pod start: the git-managed config/immich.json plus the Google
// OAuth client from Doppler (GOOGLE_OAUTH_JSON via the immich-oauth ExternalSecret). The secret never
// lands in git or a ConfigMap; the result lives in a memory-backed emptyDir readable only by the pod.
// Fails (and keeps the pod from starting) if the template or either value is missing.
const fs = require("fs");

const src = process.env.TEMPLATE || "/config-template/immich.json";
const dst = process.env.TARGET || "/config/immich.json";
const config = JSON.parse(fs.readFileSync(src, "utf8"));
const clientId = (process.env.OAUTH_CLIENT_ID || "").trim();
const clientSecret = (process.env.OAUTH_CLIENT_SECRET || "").trim();
if (!config.oauth || !config.oauth.enabled) {
  throw new Error("oauth.enabled is not true in the template; nothing to render");
}
if (!clientId || !clientSecret) {
  throw new Error("OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET missing (ExternalSecret immich-oauth)");
}
config.oauth.clientId = clientId;
config.oauth.clientSecret = clientSecret;
fs.writeFileSync(dst, JSON.stringify(config, null, 2) + "\n", { mode: 0o600 });
console.log(`rendered ${dst} (oauth client ${clientId.slice(0, 6)}…)`);
