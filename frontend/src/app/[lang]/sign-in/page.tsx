import { Suspense } from "react";
import { notFound, redirect } from "next/navigation";
import { LanguageSwitch } from "@/components/LanguageSwitch";
import { SignInForm } from "@/components/SignInForm";
import { hasLocale, type Locale } from "@/i18n/config";
import { getDictionary } from "@/i18n/dictionaries";
import { getPrincipal } from "@/lib/session";

async function AlreadySignedIn({ lang }: { lang: Locale }) {
  const p = await getPrincipal();
  if (p) redirect(p.must_change_password ? `/${lang}/change-password` : `/${lang}`);
  return null;
}

export default async function SignInPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <main>
      <Suspense fallback={null}>
        <AlreadySignedIn lang={lang} />
      </Suspense>
      <LanguageSwitch lang={lang} label={d.lang.label} path="/sign-in" />
      <h1>{d.app.name}</h1>
      <p className="muted">{d.app.tagline}</p>
      <p className="banner" role="note">
        {d.signIn.demoNote}
      </p>
      <h2>{d.signIn.title}</h2>
      <p className="muted">{d.signIn.intro}</p>
      <SignInForm t={d.signIn} errors={d.errors} />
    </main>
  );
}
