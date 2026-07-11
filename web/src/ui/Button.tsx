import { Button as AriaButton, type ButtonProps } from "react-aria-components";

const VARIANT_CLASSES = {
  primary:
    "bg-brand-600 text-white hover:bg-brand-700 data-[pressed]:bg-brand-700 " +
    "dark:bg-brand-500 dark:hover:bg-brand-600",
  secondary:
    "border border-gray-300 bg-white text-gray-700 hover:bg-gray-50 " +
    "dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800",
  ghost:
    "text-gray-500 hover:bg-gray-100 hover:text-gray-700 " +
    "dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200",
} as const;

export interface UiButtonProps extends Omit<ButtonProps, "className"> {
  variant?: keyof typeof VARIANT_CLASSES;
  testId?: string;
}

/** The one button. Variants cover every current chrome use; no size axis yet (YAGNI). */
export function Button({ variant = "secondary", testId, ...props }: UiButtonProps) {
  return (
    <AriaButton
      {...props}
      data-testid={testId}
      className={
        "inline-flex cursor-pointer items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm " +
        "font-medium outline-offset-2 transition-colors disabled:cursor-not-allowed " +
        `disabled:opacity-50 ${VARIANT_CLASSES[variant]}`
      }
    />
  );
}
