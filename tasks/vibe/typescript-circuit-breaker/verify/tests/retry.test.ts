import { describe, it, expect } from "vitest";
import { retry } from "../lib/retry";

const instantSleep = () => Promise.resolve();

describe("retry", () => {
  it("calls fn exactly maxAttempts times when it always fails", async () => {
    let calls = 0;
    const fn = () => { calls += 1; return Promise.reject(new Error("nope")); };
    await expect(
      retry(fn, { maxAttempts: 3, baseDelayMs: 1, maxDelayMs: 10, sleep: instantSleep })
    ).rejects.toThrow("nope");
    expect(calls).toBe(3);
  });

  it("returns as soon as fn succeeds and stops retrying", async () => {
    let calls = 0;
    const fn = () => {
      calls += 1;
      return calls < 3 ? Promise.reject(new Error("again")) : Promise.resolve("done");
    };
    const r = await retry(fn, { maxAttempts: 5, baseDelayMs: 1, maxDelayMs: 10, sleep: instantSleep });
    expect(r).toBe("done");
    expect(calls).toBe(3);
  });

  it("caps backoff at maxDelayMs and waits between (not after) attempts", async () => {
    const delays: number[] = [];
    const sleep = (ms: number) => { delays.push(ms); return Promise.resolve(); };
    const fn = () => Promise.reject(new Error("fail"));
    await expect(
      retry(fn, { maxAttempts: 5, baseDelayMs: 100, maxDelayMs: 500, sleep })
    ).rejects.toThrow("fail");
    // 5 attempts -> 4 waits; exponential 100,200,400,800 capped to 100,200,400,500.
    expect(delays).toEqual([100, 200, 400, 500]);
  });

  it("does not retry when shouldRetry returns false", async () => {
    let calls = 0;
    const fn = () => { calls += 1; return Promise.reject(new Error("fatal")); };
    await expect(
      retry(fn, {
        maxAttempts: 5, baseDelayMs: 1, maxDelayMs: 10,
        sleep: instantSleep, shouldRetry: () => false,
      })
    ).rejects.toThrow("fatal");
    expect(calls).toBe(1);
  });
});
