/** Synthetic-only scope and metadata tests; no live CRM connection.
 * 仅使用合成样本测试范围与元数据，不连接真实 CRM。 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { classifyUrl, createSession, drainNetwork, redactUrl, capturePageState } from "../runtime.mjs";
import { captureJsonGet } from "../api_capture.mjs";
import { capturePageFetchGet } from "../page_fetch.mjs";
import { captureResourceGet } from "../cdp_resource.mjs";
import { captureStaticResources } from "../static_capture.mjs";

const excluded = [
  "https://crm.xiaoman.cn/ciq/info?id=7",
  "https://crm.xiaoman.cn/API/ciqRead/detail?id=7",
  "https://crm.xiaoman.cn/api/ciqRead/",
  "https://crm.xiaoman.cn/crm/customer/personal?tab=seaData",
  "https://crm.xiaoman.cn/api/aiWordRead/wordCloud"
];

test("manual trade routes are excluded from automatic classification", () => {
  for (const url of excluded) assert.equal(classifyUrl(url), "excluded_manual_trade");
  assert.equal(classifyUrl("https://crm.xiaoman.cn/api/mailRead/info"), "api");
});

test("every automatic helper refuses before touching the browser", async () => {
  const fail = () => { throw new Error("browser accessed before scope rejection"); };
  const cdp = { send: fail };
  const tab = { goto: fail };
  for (const url of excluded) {
    await assert.rejects(captureJsonGet(tab, {}, { companyId: "7" }, url, "synthetic"), /manual trade/);
    await assert.rejects(capturePageFetchGet({ cdp, url }), /manual trade/);
    await assert.rejects(captureResourceGet({ cdp, url }), /manual trade/);
    await assert.rejects(captureStaticResources({ cdp, urls: [url] }), /manual trade/);
    await assert.rejects(capturePageState({ url: async () => url, title: fail }, {}, "synthetic"), /manual trade/);
  }
});

test("network capture never requests trade bodies, including redirects", async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "capture-synthetic-"));
  try {
    const session = await createSession({ caseRoot: temporary, companyId: "7", rootUrl: "https://synthetic.invalid/" });
    const events = excluded.flatMap((url, index) => [
      { method: "Network.requestWillBeSent", sequence: index * 2, params: { requestId: String(index), request: { url, method: "GET" } } },
      { method: "Network.responseReceived", sequence: index * 2 + 1, params: { requestId: String(index), response: { url: index === 0 ? "https://crm.xiaoman.cn/api/mailRead/info" : url, headers: {}, status: 200 } } }
    ]);
    const calls = [];
    const cdp = { readEvents: async () => ({ events, cursor: 20, hasMore: false }), send: async method => { calls.push(method); throw new Error("unexpected body access"); } };
    const result = await drainNetwork(cdp, session, 0, "synthetic");
    assert.deepEqual(calls, []);
    assert.equal(result.storedBodyCount, 0);
    assert.equal(result.failedBodyCount, 0);
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
});

test("signed metadata is sanitized while duplicate parameters and request URL survive", () => {
  const keys = ["X-Amz-Credential", "X-Amz-Signature", "X-Amz-Security-Token", "X-Goog-Credential", "X-Goog-Signature", "OSSAccessKeyId", "x-oss-security-token", "Policy", "Expires", "Security-Token"];
  const original = new URL("https://synthetic.invalid/object?plain=keep");
  for (const key of keys) original.searchParams.append(key, "synthetic-value");
  original.searchParams.append("X-Amz-Signature", "synthetic-second-value");
  const raw = original.href;
  const sanitized = new URL(redactUrl(raw));
  for (const key of keys) assert.match(sanitized.searchParams.get(key), /^<redacted:sha256:/);
  assert.equal(sanitized.searchParams.getAll("X-Amz-Signature").length, 2);
  assert.equal(sanitized.searchParams.get("plain"), "keep");
  assert.equal(original.href, raw);
  assert.equal(original.searchParams.get("X-Amz-Signature"), "synthetic-value");
});
