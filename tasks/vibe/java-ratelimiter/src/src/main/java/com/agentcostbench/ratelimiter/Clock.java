package com.agentcostbench.ratelimiter;

/**
 * Monotonic millisecond time source, injected so rate limiters are
 * deterministically testable. Production code passes a clock backed by
 * {@code System.currentTimeMillis()} / {@code System.nanoTime()}; tests pass a
 * controllable clock.
 */
public interface Clock {
    long millis();
}
