"use client";

import { useEffect } from "react";

import { useUI } from "./store";

// Applies data-theme to <html> and follows the OS live (replicates the design's
// matchMedia listener + resolvedTheme). Cycle order dark → light → system → dark.
export function ThemeController() {
  const theme = useUI((s) => s.theme);
  const systemDark = useUI((s) => s.systemDark);
  const setSystemDark = useUI((s) => s.setSystemDark);

  useEffect(() => {
    if (!window.matchMedia) return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    setSystemDark(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setSystemDark(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [setSystemDark]);

  useEffect(() => {
    const resolved = theme === "system" ? (systemDark ? "dark" : "light") : theme;
    document.documentElement.setAttribute("data-theme", resolved);
  }, [theme, systemDark]);

  return null;
}
