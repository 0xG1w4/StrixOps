"use client";

/* ============================================================================
   Providers — theme (dark default) + i18n (zh-CN default) + toaster.

   next-themes with attribute="data-theme" matches the CSS channel blocks
   (:root[data-theme="dark"] / [data-theme="light"]).
   ========================================================================= */

import * as React from "react";
import { ThemeProvider } from "next-themes";
import { Toaster } from "sonner";
import { I18nProvider } from "@/lib/i18n";
import AuthGate from "@/components/auth/AuthGate";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider
      attribute="data-theme"
      defaultTheme="dark"
      enableSystem={false}
      disableTransitionOnChange
    >
      <I18nProvider>
        <AuthGate>{children}</AuthGate>
        <Toaster
          position="top-right"
          toastOptions={{
            style: {
              background: "var(--bg-panel-solid)",
              border: "1px solid var(--border-primary)",
              color: "var(--text-primary)",
              fontFamily: "var(--font-sans)",
            },
          }}
        />
      </I18nProvider>
    </ThemeProvider>
  );
}
