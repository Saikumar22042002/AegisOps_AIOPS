"use client";

import { AuthProvider } from "../lib/auth";
import { ThemeController } from "../lib/theme";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <ThemeController />
      {children}
    </AuthProvider>
  );
}
