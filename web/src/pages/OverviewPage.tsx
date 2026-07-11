// SCAFFOLD (Plan 3 replaces this body with the fleet grid): element
// sections + plain subject links prove the data wiring, routing and the
// per-session lab rendering end-to-end.
import { Link } from "wouter";

import { useActiveSession } from "../data/reviewStore";

export function OverviewPage() {
  const session = useActiveSession();
  if (!session) return null;
  return (
    <main data-testid="overview-page" className="flex flex-col gap-6 p-4">
      {session.elements.map((el) => (
        <section key={el.id} data-testid={`element-section-${el.id}`}>
          <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <span aria-hidden>{el.type === "physical" ? "▦" : "▤"}</span>
            {el.id}
            <span className="font-normal text-gray-400">
              {el.hostIds.length} host{el.hostIds.length === 1 ? "" : "s"}
              {el.description ? ` · ${el.description}` : ""}
            </span>
          </h2>
          <ul className="flex flex-wrap gap-2">
            {el.hostIds.map((hostId) => (
              <li key={hostId}>
                <Link
                  href={`/host/${hostId}`}
                  data-testid={`subject-link-${hostId}`}
                  className="inline-block rounded-lg border border-gray-200 px-3 py-2 text-sm
                    hover:border-brand-500 dark:border-gray-800 dark:hover:border-brand-500"
                >
                  {hostId}
                </Link>
              </li>
            ))}
            {el.hostIds.length === 0 && (
              <li className="text-sm text-gray-400">empty — no hosts fitted</li>
            )}
          </ul>
        </section>
      ))}
    </main>
  );
}
