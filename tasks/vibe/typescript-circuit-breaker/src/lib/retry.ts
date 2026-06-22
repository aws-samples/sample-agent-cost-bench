export interface RetryOptions {
  /** Maximum number of times `fn` is invoked (NOT the number of retries). */
  maxAttempts: number;
  /** Base backoff delay in ms (first retry waits ~baseDelayMs). */
  baseDelayMs: number;
  /** Upper bound on any single backoff delay in ms. */
  maxDelayMs: number;
  /** Injected sleep; defaults to setTimeout. Tests pass a stub. */
  sleep?: (ms: number) => Promise<void>;
  /** Whether to retry after a given error. Defaults to always retry. */
  shouldRetry?: (err: unknown) => boolean;
}

const defaultSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Run `fn`, retrying with exponential backoff on failure.
 *
 * `fn` is called at most `maxAttempts` times. Between attempts the helper waits
 * `min(maxDelayMs, baseDelayMs * 2^(attempt-1))` ms. On the final failure the
 * last error is thrown. There is no wait after the final attempt.
 */
export async function retry<T>(fn: () => Promise<T>, opts: RetryOptions): Promise<T> {
  const sleep = opts.sleep ?? defaultSleep;
  const shouldRetry = opts.shouldRetry ?? (() => true);

  let lastError: unknown;
  for (let attempt = 0; attempt <= opts.maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastError = err;
      if (!shouldRetry(err)) {
        break;
      }
      const delay = opts.baseDelayMs * 2 ** attempt;
      await sleep(delay);
    }
  }
  throw lastError;
}
