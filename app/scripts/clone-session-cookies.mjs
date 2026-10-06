import fs from "node:fs/promises";
import path from "node:path";

const [endpoint, output] = process.argv.slice(2);
if (!endpoint || !output) throw new Error("usage: node clone-session-cookies.mjs <cdp-endpoint> <output-json>");

const version = await fetch(`${endpoint.replace(/\/$/, "")}/json/version`, { signal: AbortSignal.timeout(5_000) });
if (!version.ok) throw new Error(`CDP version HTTP ${version.status}`);
const { webSocketDebuggerUrl } = await version.json();
if (typeof webSocketDebuggerUrl !== "string") throw new Error("CDP websocket URL missing");

async function cdpCall(socketUrl, method) {
  return new Promise((resolve, reject) => {
  const socket = new WebSocket(socketUrl);
  const timer = setTimeout(() => {
    socket.close();
    reject(new Error("CDP cookie export timeout"));
  }, 10_000);
  socket.addEventListener("open", () => socket.send(JSON.stringify({ id: 1, method })));
  socket.addEventListener("message", event => {
    const message = JSON.parse(String(event.data));
    if (message.id !== 1) return;
    clearTimeout(timer);
    socket.close();
    if (message.error) reject(new Error(message.error.message ?? `${method} failed`));
    else resolve(message.result ?? {});
  });
  socket.addEventListener("error", () => {
    clearTimeout(timer);
    reject(new Error("CDP websocket error"));
  });
  });
}

let response = await cdpCall(webSocketDebuggerUrl, "Storage.getCookies");
if (!Array.isArray(response.cookies) || response.cookies.length === 0) {
  const targetResponse = await fetch(`${endpoint.replace(/\/$/, "")}/json/list`, { signal: AbortSignal.timeout(5_000) });
  if (!targetResponse.ok) throw new Error(`CDP targets HTTP ${targetResponse.status}`);
  const targets = await targetResponse.json();
  const target = Array.isArray(targets) ? targets.find(item => {
    try {
      const host = new URL(String(item.url ?? "")).hostname.toLowerCase();
      return item.type === "page" && (host === "xiaoman.cn" || host.endsWith(".xiaoman.cn"));
    } catch { return false; }
  }) : null;
  if (typeof target?.webSocketDebuggerUrl === "string") {
    response = await cdpCall(target.webSocketDebuggerUrl, "Network.getAllCookies");
  }
}

const cookies = Array.isArray(response.cookies) ? response.cookies : [];
const allowed = cookies
  .filter(cookie => {
    const host = String(cookie.domain ?? "").replace(/^\./, "").toLowerCase();
    return host === "xiaoman.cn" || host.endsWith(".xiaoman.cn");
  })
  .map(cookie => ({
    name: String(cookie.name ?? ""),
    value: String(cookie.value ?? ""),
    domain: String(cookie.domain ?? ""),
    path: String(cookie.path ?? "/"),
    secure: cookie.secure !== false,
    httpOnly: cookie.httpOnly === true,
    expirationDate: Number(cookie.expires ?? -1),
    sameSite: String(cookie.sameSite ?? "unspecified")
  }));

await fs.mkdir(path.dirname(output), { recursive: true });
const temporary = `${output}.${process.pid}.tmp`;
await fs.writeFile(temporary, JSON.stringify(allowed), { encoding: "utf8", mode: 0o600 });
await fs.rename(temporary, output);
process.stdout.write(`cookie_bootstrap_count=${allowed.length}\n`);
