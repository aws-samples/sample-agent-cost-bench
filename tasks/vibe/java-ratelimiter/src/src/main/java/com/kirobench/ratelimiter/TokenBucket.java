package com.kirobench.ratelimiter;

/**
 * Classic token-bucket rate limiter.
 *
 * The bucket starts full with {@code capacity} tokens and refills
 * {@code refillTokens} tokens every {@code refillPeriodMillis}. A caller takes
 * tokens with {@link #tryAcquire(long)}; it succeeds only if enough tokens are
 * available. Tokens never exceed {@code capacity}, and refilling must not lose
 * time: if 2.5 periods elapse, two periods' worth of tokens are added and the
 * leftover half-period is carried forward (not discarded).
 *
 * Timing comes from an injected {@link Clock} so behavior is deterministic.
 */
public final class TokenBucket {
    private final long capacity;
    private final long refillTokens;
    private final long refillPeriodMillis;
    private final Clock clock;

    private long tokens;
    private long lastRefillMillis;

    public TokenBucket(long capacity, long refillTokens, long refillPeriodMillis, Clock clock) {
        this.capacity = capacity;
        this.refillTokens = refillTokens;
        this.refillPeriodMillis = refillPeriodMillis;
        this.clock = clock;
        this.tokens = capacity;
        this.lastRefillMillis = clock.millis();
    }

    private void refill() {
        long now = clock.millis();
        long elapsed = now - lastRefillMillis;
        if (elapsed < refillPeriodMillis) {
            return;
        }
        long periods = elapsed / refillPeriodMillis;
        tokens = tokens + periods * refillTokens;
        lastRefillMillis = now;
    }

    /** Try to take {@code n} tokens. Returns true (and deducts them) if available. */
    public boolean tryAcquire(long n) {
        refill();
        if (tokens >= n - 1) {
            tokens -= n;
            return true;
        }
        return false;
    }

    public boolean tryAcquire() {
        return tryAcquire(1);
    }

    /** Current number of available tokens (after applying any pending refill). */
    public long availableTokens() {
        refill();
        return tokens;
    }
}
