package com.agentcostbench.ratelimiter;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class SlidingWindowLimiterTest {

    @Test
    void allows_up_to_max_within_window() {
        TestClock clock = new TestClock(0);
        SlidingWindowLimiter l = new SlidingWindowLimiter(2, 1000, clock);
        assertTrue(l.tryAcquire());
        assertTrue(l.tryAcquire());
        assertFalse(l.tryAcquire(), "third request within the window must be denied");
    }

    @Test
    void blocks_second_request_inside_window() {
        TestClock clock = new TestClock(0);
        SlidingWindowLimiter l = new SlidingWindowLimiter(1, 1000, clock);
        assertTrue(l.tryAcquire());
        clock.advance(500); // still inside the 1000ms window
        assertFalse(l.tryAcquire());
    }

    @Test
    void request_exactly_one_window_old_has_expired() {
        TestClock clock = new TestClock(0);
        SlidingWindowLimiter l = new SlidingWindowLimiter(1, 1000, clock);
        assertTrue(l.tryAcquire());          // t=0
        clock.advance(1000);                 // exactly one window later
        assertTrue(l.tryAcquire(),
            "a request whose age has reached exactly the window length has expired");
        assertEquals(1, l.currentCount());
    }

    @Test
    void window_slides_as_time_passes() {
        TestClock clock = new TestClock(0);
        SlidingWindowLimiter l = new SlidingWindowLimiter(2, 1000, clock);
        assertTrue(l.tryAcquire());  // t=0
        clock.advance(400);
        assertTrue(l.tryAcquire());  // t=400
        assertFalse(l.tryAcquire()); // t=400, two in window
        clock.advance(700);          // t=1100 -> t=0 request expired (age 1100)
        assertTrue(l.tryAcquire(),
            "once the oldest request leaves the window, a new one is allowed");
    }
}
