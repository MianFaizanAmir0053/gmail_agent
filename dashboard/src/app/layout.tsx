import type { Metadata, Viewport } from "next";
import Link from "next/link";

import { ControlBar } from "@/components/ControlBar";
import "./globals.css";

export const metadata: Metadata = {
  title: "mailagent",
  description: "Traces, cost, and evaluation history",
  // iOS reads these when the page is added to the Home Screen. The icon sits
  // under `/icons/`, which the sign-in gate leaves open.
  appleWebApp: { capable: true, title: "mailagent", statusBarStyle: "default" },
  icons: { apple: "/icons/apple-touch-icon.png" },
};

export const viewport: Viewport = {
  themeColor: "#0f172a",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          {/* No prefetching: every page is dynamic, so a prefetch runs its
              queries -- and the timeline re-reads every few seconds while a
              decision is applied, which would re-prefetch them all each time. */}
          <nav>
            <span className="brand">mailagent</span>
            <Link href="/" prefetch={false}>
              Timeline
            </Link>
            <Link href="/analytics" prefetch={false}>
              Runs
            </Link>
            <Link href="/costs" prefetch={false}>
              Costs
            </Link>
            <Link href="/evals" prefetch={false}>
              Evals
            </Link>
            <Link href="/failures" prefetch={false}>
              Failures
            </Link>
            <Link href="/activity" prefetch={false}>
              Activity
            </Link>
            <ControlBar />
          </nav>
          {children}
        </div>
      </body>
    </html>
  );
}
