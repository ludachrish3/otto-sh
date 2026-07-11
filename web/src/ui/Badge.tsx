import type { ReactNode } from "react";

const TONE_CLASSES = {
  historical: "bg-status-historical/15 text-status-historical dark:bg-status-historical/25",
  neutral: "bg-gray-200 text-gray-600 dark:bg-gray-800 dark:text-gray-300",
} as const;

export function Badge({
  tone = "neutral",
  testId,
  children,
}: {
  tone?: keyof typeof TONE_CLASSES;
  testId?: string;
  children: ReactNode;
}) {
  return (
    <span
      data-testid={testId}
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-semibold
        tracking-wide ${TONE_CLASSES[tone]}`}
    >
      {children}
    </span>
  );
}
