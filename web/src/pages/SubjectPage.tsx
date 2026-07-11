// SCAFFOLD (Plan 3 replaces this with the synced chart stack): proves
// subject resolution, deep links and range-scoped data selection.
import { Link, useParams } from "wouter";

import { metricsForSubject, subjectKind } from "../data/exportDoc";
import { useActiveSession, useReviewStore } from "../data/reviewStore";

export function SubjectPage() {
  const params = useParams<{ id: string }>();
  const session = useActiveSession();
  const range = useReviewStore((s) => s.range);
  if (!session) return null;

  const id = params.id;
  const kind = subjectKind(session, id);
  if (kind === null) {
    return (
      <main data-testid="not-found" className="p-4 text-sm text-gray-500">
        Unknown subject "{id}" in this session. <Link href="/">Back to overview</Link>
      </main>
    );
  }

  const host = session.lab.hosts.find((h) => h.id === id);
  const metrics = metricsForSubject(session, id, range);
  const labels = [...new Set(metrics.map((m) => m.label))].sort();

  return (
    <main data-testid="subject-page" className="flex flex-col gap-4 p-4">
      <nav className="text-sm text-gray-400">
        <Link href="/">Fleet</Link> / {id}
      </nav>
      <h1 data-testid="subject-title" className="flex items-center gap-2 text-lg font-semibold">
        {id}
        <span className="text-sm font-normal text-gray-400">
          {kind}
          {host?.board ? ` · ${host.board}` : ""}
          {host?.slot != null ? ` · slot ${host.slot}` : ""}
          {host?.hop ? ` · via ${host.hop}` : ""}
        </span>
      </h1>
      <p data-testid="series-summary" className="text-sm text-gray-500 dark:text-gray-400">
        {labels.length} series · {metrics.length} samples in range
      </p>
      <ul className="text-sm text-gray-600 dark:text-gray-300">
        {labels.map((label) => (
          <li key={label}>
            {label} ({metrics.filter((m) => m.label === label).length})
          </li>
        ))}
      </ul>
    </main>
  );
}
