package com.kirobench.ratelimiter;

/** A controllable clock for deterministic rate-limiter tests. */
final class TestClock implements Clock {
    private long now;

    TestClock(long start) {
        this.now = start;
    }

    @Override
    public long millis() {
        return now;
    }

    void advance(long deltaMillis) {
        now += deltaMillis;
    }

    void setMillis(long value) {
        now = value;
    }
}
