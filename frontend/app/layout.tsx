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
  title: "StockPilot",
  description: "AI-assisted restocking for independent retailers",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body>
        <nav className="nav" aria-label="Main">
          <Link href="/">StockPilot</Link>
          <Link href="/">Chat</Link>
          <Link href="/promotions">Promotions</Link>
          <Link href="/orders">Order history</Link>
        </nav>
        {children}
      </body>
    </html>
  );
}
