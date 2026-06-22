package com.kirobench.ratelimiter;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class TokenBucketTest {

    @Test
    void starts_full_and_acquires_up_to_capacity() {
        TestClock clock = new TestClock(0);
        TokenBucket b = new TokenBucket(3, 1, 100, clock);
        assertTrue(b.tryAcquire(1));
        assertTrue(b.tryAcquire(1));
        assertTrue(b.tryAcquire(1));
        assertFalse(b.tryAcquire(1), "bucket should be empty after taking all tokens");
    }

    @Test
    void tryAcquire_does_not_allow_one_beyond_available() {
        TestClock clock = new TestClock(0);
        TokenBucket b = new TokenBucket(2, 1, 100, clock);
        assertTrue(b.tryAcquire(2));      // takes both, now 0
        assertFalse(b.tryAcquire(1), "must not grant a token when none remain");
    }

    @Test
    void refill_is_capped_at_capacity() {
        TestClock clock = new TestClock(0);
        TokenBucket b = new TokenBucket(5, 1, 100, clock);
        clock.advance(1000); // 10 periods worth, but bucket is already full
        assertEquals(5, b.availableTokens(), "tokens must never exceed capacity");
    }

    @Test
    void refill_carries_forward_leftover_time() {
        TestClock clock = new TestClock(0);
        TokenBucket b = new TokenBucket(100, 1, 100, clock);
        // Drain to 0.
        for (int i = 0; i < 100; i++) {
            assertTrue(b.tryAcquire(1));
        }
        assertEquals(0, b.availableTokens());

        clock.advance(150); // 1 full period (+1), 50ms remainder carried forward
        assertEquals(1, b.availableTokens());

        clock.advance(50);  // remainder + 50 = another full period (+1)
        assertEquals(2, b.availableTokens(),
            "leftover time from the previous refill must be carried forward");
    }

    @Test
    void partial_period_adds_nothing() {
        TestClock clock = new TestClock(0);
        TokenBucket b = new TokenBucket(10, 1, 100, clock);
        b.tryAcquire(10); // empty
        clock.advance(99);
        assertEquals(0, b.availableTokens(), "less than one period adds no tokens");
    }
}
