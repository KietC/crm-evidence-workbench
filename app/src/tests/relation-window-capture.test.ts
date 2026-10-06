import test from "node:test";
import assert from "node:assert/strict";
import { validateProbeRequest, validateRelationWindowPayload } from "../relation-window-capture.js";

const good = () => ({
  code: 0,
  msg: "ok",
  now: 1,
  data: {
    previous: [{ mail_id: "10", user_id: "1" }],
    next: [{ mail_id: 20, user_id: null }],
    total: 2
  }
});

test("relation window payload accepts the exact observed contract", () => {
  const payload = validateRelationWindowPayload(good(), 2);
  assert.equal(payload.data.previous[0]?.mail_id, "10");
  assert.equal(payload.data.next[0]?.mail_id, "20");
});

test("relation window payload rejects drift and invalid ids", () => {
  assert.throws(() => validateRelationWindowPayload({ ...good(), extra: true }, 2), /keys invalid/);
  const badCode = good(); badCode.code = 1219;
  assert.throws(() => validateRelationWindowPayload(badCode, 2), /code invalid/);
  const badId = good(); badId.data.next[0] = { mail_id: 0, user_id: null };
  assert.throws(() => validateRelationWindowPayload(badId, 2), /mail_id invalid/);
  const badTotal = good(); badTotal.data.total = 1;
  assert.throws(() => validateRelationWindowPayload(badTotal, 2), /total invalid/);
});

test("probe request is bound to endpoint and company while allowing a live anchor", () => {
  const request = {
    method: "GET",
    headers: { Accept: "application/json" },
    url: "https://crm.xiaoman.cn/api/customerRead/mailPreviousNext?company_id=7&curPage=1&pageSize=100&mail_id=10"
  };
  const url = validateProbeRequest(request, "7");
  assert.equal(url.pathname, "/api/customerRead/mailPreviousNext");
  assert.throws(() => validateProbeRequest(request, "8"), /company_id mismatch/);
  assert.throws(() => validateProbeRequest({ ...request, method: "POST" }, "7"), /method invalid/);
  assert.equal(validateProbeRequest({ ...request, url: request.url.replace("mail_id=10", "mail_id=99") }, "7").searchParams.get("mail_id"), "99");
});
