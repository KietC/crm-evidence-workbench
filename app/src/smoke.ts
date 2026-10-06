import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadAdapter, parseCustomerUrl } from "./adapter.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(here, "..");
const adapter = await loadAdapter();
const problems: string[] = [];

for (const required of [
  path.join(appRoot, "ui", "index.html"),
  path.join(appRoot, "ui", "app.js"),
  path.join(appRoot, "ui", "styles.css"),
  path.join(appRoot, "adapters", "okki", "v1", "adapter.json")
]) {
  try { await fs.access(required); } catch { problems.push(`missing: ${required}`); }
}

const sample = parseCustomerUrl("https://crm.xiaoman.cn/crm/customer/personal?company_id=123456", adapter);
if (sample?.companyId !== "123456") problems.push("customer URL parser failed");
if (parseCustomerUrl("https://crm.xiaoman.cn/crm/customer/personal?company_id=abc", adapter)) problems.push("invalid company id accepted");
if (parseCustomerUrl("https://crm.xiaoman.cn/crm/customer/list?company_id=123", adapter)) problems.push("out-of-scope path accepted");

if (problems.length) {
  process.stderr.write(`${JSON.stringify({ status: "failed", problems }, null, 2)}\n`);
  process.exit(1);
}
process.stdout.write(`${JSON.stringify({
  status: "passed",
  adapter: adapter.adapter_id,
  rootTabs: adapter.root_tabs.length,
  endpointContracts: adapter.expected_endpoint_contracts.length
}, null, 2)}\n`);
