import { Input, Label, TextField } from "react-aria-components";

export function TextInput({
  label,
  type = "text",
  value,
  onChange,
  testId,
}: {
  label: string;
  type?: string;
  value: string;
  onChange: (value: string) => void;
  testId?: string;
}) {
  return (
    <TextField value={value} onChange={onChange} className="inline-flex items-center gap-1.5">
      <Label className="text-xs text-gray-500 dark:text-gray-400">{label}</Label>
      <Input
        type={type}
        data-testid={testId}
        className="rounded-lg border border-gray-300 bg-white px-2 py-1 text-sm
          text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
      />
    </TextField>
  );
}
