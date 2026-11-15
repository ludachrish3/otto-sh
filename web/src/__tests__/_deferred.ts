/** A promise and the functions that settle it, for a test to hold a step
 * (a fetch, a file read, a write) open and settle it by hand. */
export interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
}

/** A promise the test settles by hand (`Deferred`). */
export function deferred<T = void>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
