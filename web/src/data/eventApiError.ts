// web/src/data/eventApiError.ts
// The error eventApi.ts throws for a failed mutation (one class per file),
// and the message a refusal's body is read as.
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

/** The message for a request that never reached the server. */
export function networkErrorMessage(err: unknown): string {
  return `Network error: ${String(err)}`;
}

/** The message a refusal's JSON body carries: its `error`, else FastAPI's
 * `detail` (a string, or a body-validation list's first `msg`), else the
 * status. Every request to the server reads a refusal's message here. */
export function errorMessage(parsed: unknown, status: number): string {
  const body =
    typeof parsed === "object" && parsed !== null
      ? (parsed as { error?: unknown; detail?: unknown })
      : null;
  if (typeof body?.error === "string") return body.error;
  // FastAPI body-validation failures arrive as {"detail": [{msg, ...}]}.
  if (typeof body?.detail === "string") return body.detail;
  if (Array.isArray(body?.detail)) {
    const msg = (body.detail[0] as { msg?: unknown } | undefined)?.msg;
    if (typeof msg === "string") return msg;
  }
  return `Request failed (${status})`;
}
