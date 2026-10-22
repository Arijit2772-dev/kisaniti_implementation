import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "KisanNiti",
  description: "Decision support for FPO post-harvest selling",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
