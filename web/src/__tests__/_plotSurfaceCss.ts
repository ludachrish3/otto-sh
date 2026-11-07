// jsdom loads no stylesheet, so theme.ts's plotSurfaces() (which reads
// --color-bg-primary from the CSSOM and fails loudly when it is missing) has
// nothing to read. This installs the same SHAPE the built app.css has --
// Tailwind's `:root,:host` theme rule, and theme.css's `.dark-mode` rule,
// each value a var() reference to resolve -- with recognisable test values.
export const TEST_PLOT_SURFACES = { light: "#fefefe", dark: "#0b0b0b" } as const;

export function installPlotSurfaceCss(): void {
  if (document.querySelector("#plot-surface-test-css") !== null) return;
  const style = document.createElement("style");
  style.id = "plot-surface-test-css";
  style.textContent = `
    :root, :host {
      --color-test-white: ${TEST_PLOT_SURFACES.light};
      --color-test-black: ${TEST_PLOT_SURFACES.dark};
      --color-bg-primary: var(--color-test-white);
    }
    .dark-mode { --color-bg-primary: var(--color-test-black); }
  `;
  document.head.append(style);
}
