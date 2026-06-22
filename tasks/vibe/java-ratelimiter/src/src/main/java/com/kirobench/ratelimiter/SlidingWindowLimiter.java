package com.kirobench.ratelimiter;

import java.util.ArrayDeque;
import java.util.Deque;

/**
 * Sliding-window rate limiter: allows at most {@code maxRequests} within any
 * trailing window of {@code windowMillis}.
 *
 * A timestamp is "in window" while it is strictly newer than {@code windowMillis}
 * ago; a request whose age has reached exactly {@code windowMillis} has expired
 * and must no longer count. Timing comes from an injected {@link Clock}.
 */
public final class SlidingWindowLimiter {
    private final int maxRequests;
    private final long windowMillis;
    private final Clock clock;
    private final Deque<Long> timestamps = new ArrayDeque<>();

    public SlidingWindowLimiter(int maxRequests, long windowMillis, Clock clock) {
        this.maxRequests = maxRequests;
        this.windowMillis = windowMillis;
        this.clock = clock;
    }

    /** Record a request if the window has room; returns true if allowed. */
    public boolean tryAcquire() {
        long now = clock.millis();
        while (!timestamps.isEmpty() && now - timestamps.peekFirst() > windowMillis) {
            timestamps.pollFirst();
        }
        if (timestamps.size() < maxRequests) {
            timestamps.addLast(now);
            return true;
        }
        return false;
    }

    /** Number of requests currently counted within the window. */
    public int currentCount() {
        long now = clock.millis();
        while (!timestamps.isEmpty() && now - timestamps.peekFirst() > windowMillis) {
            timestamps.pollFirst();
        }
        return timestamps.size();
    }
}
