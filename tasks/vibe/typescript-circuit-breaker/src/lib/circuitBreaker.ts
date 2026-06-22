export type CircuitState = "closed" | "open" | "half_open";

export class CircuitOpenError extends Error {
  constructor() {
    super("circuit is open");
    this.name = "CircuitOpenError";
  }
}

export interface CircuitBreakerOptions {
  /** Open the circuit after this many CONSECUTIVE failures. */
  failureThreshold: number;
  /** How long to stay open before allowing a single half-open probe (ms). */
  openMillis: number;
  /** Injected clock (ms). Defaults to Date.now for production use. */
  now?: () => number;
}

/**
 * Circuit breaker.
 *
 *  - closed:    calls pass through; N consecutive failures open the circuit.
 *  - open:      calls fail fast with CircuitOpenError until `openMillis` has
 *               elapsed, after which one probe is allowed (half-open).
 *  - half_open: a single probe runs; success closes the circuit and resets the
 *               failure count, failure re-opens it.
 *
 * A success in the closed state resets the consecutive-failure count.
 */
export class CircuitBreaker {
  private readonly failureThreshold: number;
  private readonly openMillis: number;
  private readonly now: () => number;

  private _state: CircuitState = "closed";
  private failures = 0;
  private openedAt = 0;

  constructor(opts: CircuitBreakerOptions) {
    this.failureThreshold = opts.failureThreshold;
    this.openMillis = opts.openMillis;
    this.now = opts.now ?? Date.now;
  }

  get state(): CircuitState {
    return this._state;
  }

  async exec<T>(fn: () => Promise<T>): Promise<T> {
    if (this._state === "open") {
      if (this.now() - this.openedAt > this.openMillis) {
        this._state = "half_open";
      } else {
        throw new CircuitOpenError();
      }
    }

    try {
      const result = await fn();
      this.onSuccess();
      return result;
    } catch (err) {
      this.onFailure();
      throw err;
    }
  }

  private onSuccess(): void {
    this._state = "closed";
  }

  private onFailure(): void {
    if (this._state === "half_open") {
      this._state = "open";
      this.openedAt = this.now();
      return;
    }
    this.failures += 1;
    if (this.failures >= this.failureThreshold) {
      this._state = "open";
      this.openedAt = this.now();
    }
  }
}
