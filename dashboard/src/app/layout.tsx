import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "mailagent",
  description: "Traces, cost, and evaluation history",
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
