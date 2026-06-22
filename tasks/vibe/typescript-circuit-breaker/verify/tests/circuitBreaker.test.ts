import { describe, it, expect } from "vitest";
import { CircuitBreaker, CircuitOpenError } from "../lib/circuitBreaker";

/** A controllable clock for deterministic tests. */
function makeClock(start = 0) {
  let t = start;
  return { now: () => t, advance: (d: number) => { t += d; } };
}

const fail = () => Promise.reject(new Error("boom"));
const ok = () => Promise.resolve("ok");

describe("CircuitBreaker", () => {
  it("opens after the threshold of consecutive failures", async () => {
    const clk = makeClock();
    const cb = new CircuitBreaker({ failureThreshold: 3, openMillis: 1000, now: clk.now });
    for (let i = 0; i < 3; i++) await expect(cb.exec(fail)).rejects.toThrow("boom");
    expect(cb.state).toBe("open");
  });

  it("fails fast while open before the timeout elapses", async () => {
    const clk = makeClock();
    const cb = new CircuitBreaker({ failureThreshold: 1, openMillis: 1000, now: clk.now });
    await expect(cb.exec(fail)).rejects.toThrow("boom"); // opens
    clk.advance(500);
    let called = false;
    await expect(cb.exec(async () => { called = true; return "x"; }))
      .rejects.toBeInstanceOf(CircuitOpenError);
    expect(called).toBe(false);
  });

  it("allows a probe once exactly openMillis has elapsed", async () => {
    const clk = makeClock();
    const cb = new CircuitBreaker({ failureThreshold: 1, openMillis: 1000, now: clk.now });
    await expect(cb.exec(fail)).rejects.toThrow("boom"); // opens at t=0
    clk.advance(1000); // exactly the timeout
    let called = false;
    const r = await cb.exec(async () => { called = true; return "ok"; });
    expect(called).toBe(true);
    expect(r).toBe("ok");
    expect(cb.state).toBe("closed");
  });

  it("a success in the closed state resets the consecutive-failure count", async () => {
    const clk = makeClock();
    const cb = new CircuitBreaker({ failureThreshold: 3, openMillis: 1000, now: clk.now });
    await expect(cb.exec(fail)).rejects.toThrow();
    await expect(cb.exec(fail)).rejects.toThrow(); // 2 failures
    await cb.exec(ok);                              // success resets the counter
    await expect(cb.exec(fail)).rejects.toThrow();  // 1 failure since reset
    expect(cb.state).toBe("closed");
  });

  it("recovers cleanly from half-open: needs a fresh threshold to re-open", async () => {
    const clk = makeClock();
    const cb = new CircuitBreaker({ failureThreshold: 2, openMillis: 1000, now: clk.now });
    await expect(cb.exec(fail)).rejects.toThrow();
    await expect(cb.exec(fail)).rejects.toThrow(); // open at t=0
    clk.advance(1000);
    await cb.exec(ok);                 // half-open probe succeeds -> closed, counter reset
    expect(cb.state).toBe("closed");
    await expect(cb.exec(fail)).rejects.toThrow(); // only 1 failure -> still closed
    expect(cb.state).toBe("closed");
  });

  it("a failed half-open probe re-opens the circuit", async () => {
    const clk = makeClock();
    const cb = new CircuitBreaker({ failureThreshold: 1, openMillis: 1000, now: clk.now });
    await expect(cb.exec(fail)).rejects.toThrow(); // open
    clk.advance(1000);
    await expect(cb.exec(fail)).rejects.toThrow(); // half-open probe fails
    expect(cb.state).toBe("open");
  });
});
