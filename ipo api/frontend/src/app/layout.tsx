import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "IPO Copilot & Cashflow Scheduler",
  description:
    "Deterministic ASBA capital-allocation planning across multiple PANs. Not financial advice.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
