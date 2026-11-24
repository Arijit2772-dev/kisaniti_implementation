import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { LOCALES, hasLocale } from "@/i18n/config";
import { getDictionary } from "@/i18n/dictionaries";
import "../globals.css";

export function generateStaticParams() {
  return LOCALES.map((lang) => ({ lang }));
}

export async function generateMetadata({ params }: { params: Promise<{ lang: string }> }): Promise<Metadata> {
  const { lang } = await params;
  if (!hasLocale(lang)) return {};
  const d = await getDictionary(lang);
  return { title: d.app.name, description: d.app.description };
}

export default async function RootLayout({
  children,
  params,
}: Readonly<{ children: React.ReactNode; params: Promise<{ lang: string }> }>) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  // build identity: the commit CI built this image from (KN_BUILD_COMMIT build argument); "unknown" on local builds
  const commit = process.env.KN_BUILD_COMMIT || "unknown";
  return (
    <html lang={lang}>
      <body>
        {children}
        <footer className="build muted" data-build-commit={commit}>
          {d.app.build.replace("{commit}", commit.slice(0, 7))}
        </footer>
      </body>
    </html>
  );
}
