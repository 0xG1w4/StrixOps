import type { Metadata } from "next";
import "./globals.css";
import "./signal-grid.css";
import Shell from "@/components/Shell";
import { Providers } from "@/components/providers";

export const metadata: Metadata = {
  title: "StrixOps Console",
  description: "Autonomous pentest operations console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" data-direction="signal-grid" suppressHydrationWarning>
      <body suppressHydrationWarning>
        <Providers>
          <Shell>{children}</Shell>
        </Providers>
      </body>
    </html>
  );
}
