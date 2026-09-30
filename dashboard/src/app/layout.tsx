import type { Metadata, Viewport } from "next";
import Link from "next/link";
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
          <nav>
            <span className="brand">mailagent</span>
            <Link href="/">Runs</Link>
            <Link href="/costs">Costs</Link>
            <Link href="/evals">Evals</Link>
            <Link href="/failures">Failures</Link>
          </nav>
          {children}
        </div>
      </body>
    </html>
  );
}
