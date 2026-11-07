// web/src/data/eventApiError.ts
// The error eventApi.ts throws for a failed mutation (one class per file).
/** A failed mutation. `status` is the HTTP status (null for a network
 * failure that never reached the server); `observed` the response's
 * X-Otto-Observed-At stamp (live error responses carry it too), which a
 * recovery apply passes on so it orders against the SSE echo. */
export class EventApiError extends Error {
  readonly status: number | null;
  readonly observed: string | null;

  constructor(
    message: string,
    {
      status = null,
      observed = null,
      cause,
    }: {
      status?: number | null;
      observed?: string | null;
      cause?: unknown;
    } = {},
  ) {
    super(message, cause === undefined ? undefined : { cause });
    this.status = status;
    this.observed = observed;
  }
}
