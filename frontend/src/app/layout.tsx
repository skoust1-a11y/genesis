import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
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
  description: "Ingest strategies and run Review Crew backtests with costs",
};

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
        <nav className="border-b bg-white">
          <div className="mx-auto flex max-w-6xl gap-6 px-4 py-3 text-sm">
            <Link href="/" className="font-semibold">Trading Strategy Lab</Link>
            <Link href="/deploy" className="text-zinc-600 hover:text-zinc-900">Deploy</Link>
            <Link href="/deployments" className="text-zinc-600 hover:text-zinc-900">Deployments</Link>
          </div>
        </nav>
        {children}
      </body>
    </html>
  );
}
