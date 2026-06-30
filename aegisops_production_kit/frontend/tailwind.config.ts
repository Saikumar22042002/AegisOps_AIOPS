import type { Config } from "tailwindcss";

// Preflight is disabled: the design ships its own reset (`* { margin:0; padding:0; … }`)
// and is rendered with verbatim inline styles. Tailwind stays available for layout
// helpers without altering the pixel-exact design.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  corePlugins: { preflight: false },
  theme: { extend: {} },
  plugins: [],
};

export default config;
