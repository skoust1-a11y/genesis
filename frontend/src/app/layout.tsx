import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";

import { AuthNav } from "@/components/AuthNav";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Trading Strategy Lab",
  description: "Markets, paper trading, and strategy backtests with Indian charges",
};

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/markets", label: "Markets" },
  { href: "/lab", label: "Strategy Lab" },
  { href: "/deploy", label: "Deploy" },
  { href: "/deployments", label: "Deployments" },
];

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased bg-zinc-50 text-zinc-900`}
      >
        <nav className="sticky top-0 z-10 border-b bg-white/90 backdrop-blur">
          <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3 text-sm">
            <Link href="/" className="flex items-center gap-2 font-semibold">
              <span className="inline-block h-6 w-6 rounded-md bg-emerald-600" aria-hidden />
              Trading Strategy Lab
            </Link>
            {NAV.map((item) => (
              <Link key={item.href} href={item.href} className="text-zinc-600 hover:text-emerald-700">
                {item.label}
              </Link>
            ))}
            <span className="ml-auto rounded-full bg-amber-100 px-3 py-1 text-xs font-medium text-amber-900">
              Paper trading
            </span>
            <AuthNav />
          </div>
        </nav>
        {children}
      </body>
    </html>
  );
}
