import test from "node:test";
import assert from "node:assert/strict";
import { CaptureStartGate, CaptureBusyError, ensureCustomerIdentity, reservationExpired, reservationOwnerMatches } from "../capture-start-gate.js";
import { CaptureEngine } from "../capture-engine.js";
import type { BrowserManager } from "../browser-manager.js";
import type { OkkiAdapter } from "../types.js";

test("normal and one-shot simultaneous starters reject before any await and preserve active owner", async () => {
  for (const mode of ["normal", "one-shot"]) {
    const gate = new CaptureStartGate();
    let release!: () => void;
    let activeJob = "running";
    const first = gate.run(async () => { await new Promise<void>(resolve => { release = resolve; }); });
    await assert.rejects(gate.run(async () => { activeJob = "failed"; }), CaptureBusyError, mode);
    assert.equal(activeJob, "running");
    release(); await first;
    await gate.run(async () => {});
  }
});

test("detached capture retains owner after HTTP entry returns; timer cannot inherit it", async () => {
  const gate = new CaptureStartGate();
  let release!: () => void;
  let capture!: Promise<void>;
  await gate.run(async () => {
    capture = gate.run(async () => { await new Promise<void>(resolve => { release = resolve; }); });
    await assert.rejects(gate.run(async () => {}, false), CaptureBusyError);
  });
  await assert.rejects(gate.run(async () => {}), CaptureBusyError);
  release(); await capture;
  await assert.rejects(gate.run(async () => { throw new Error("startup"); }), /startup/);
  await gate.run(async () => {});
});

test("synthetic browser recovery navigates B to marker A and fails closed on wrong result", async () => {
  for (const mode of ["normal", "one-shot"]) {
    let current = "200";
    const visited: string[] = [];
    await ensureCustomerIdentity("100", async () => current, async id => { visited.push(id); current = id; });
    assert.deepEqual(visited, ["100"], mode);
    await ensureCustomerIdentity("100", async () => current, async () => { assert.fail("already matches"); });
    await assert.rejects(ensureCustomerIdentity("100", async () => "200", async () => {}), /mismatch/);
    await assert.rejects(ensureCustomerIdentity("", async () => current, async () => {}), /invalid/);
  }
});

test("reservation expiry protects live PID regardless of age and treats unknown conservatively", () => {
  const old = "2020-01-01T00:00:00Z";
  assert.equal(reservationExpired("alive", old), false);
  assert.equal(reservationExpired("alive", "invalid"), false);
  assert.equal(reservationExpired("dead", new Date().toISOString()), true);
  assert.equal(reservationExpired("unknown", new Date().toISOString()), false);
  assert.equal(reservationExpired("unknown", old), true);
  assert.equal(reservationExpired("unknown", "invalid"), false);
});

test("direct Engine.start reserves while synthetic customerPage is pending and releases on preflight error", async () => {
  let release!: () => void;
  const browser = { customerPage: () => new Promise<null>(resolve => { release = () => resolve(null); }) };
  const engine = new CaptureEngine("unused-synthetic", {} as OkkiAdapter, browser as unknown as BrowserManager);
  const first = engine.start();
  await assert.rejects(engine.start(), /正在运行或启动/);
  assert.equal(engine.status().running, false);
  release(); await assert.rejects(first, /没有打开/);
  const retry = engine.start();
  release(); await assert.rejects(retry, /没有打开/);
});

test("old reservation owner cannot release a replacement owner", () => {
  const expected = { instanceId: "1", pid: 42, companyId: "100", claimedAt: "old" };
  const record = { instance_id: "1", pid: 42, company_id: "100", claimed_at: "old" };
  assert.equal(reservationOwnerMatches(record, expected), true);
  assert.equal(reservationOwnerMatches({ ...record, claimed_at: "new" }, expected), false);
  assert.equal(reservationOwnerMatches({ ...record, pid: 43 }, expected), false);
  assert.equal(reservationOwnerMatches({ ...record, instance_id: "2" }, expected), false);
});

test("Engine rejects customer changed after recovery check before any evidence writes", async () => {
  const browser = { customerPage: async () => ({ url: () => "https://synthetic.invalid/customer?company_id=200" }) };
  const adapter = { origin: "https://synthetic.invalid", customer_path: "/customer" } as OkkiAdapter;
  const engine = new CaptureEngine("unused-synthetic", adapter, browser as unknown as BrowserManager);
  await assert.rejects(engine.start({ expectedCompanyId: "100" }), /身份与启动绑定不一致/);
  assert.equal(engine.status().running, false);
  assert.equal(engine.status().caseRoot, null);
});
