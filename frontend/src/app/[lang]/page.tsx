import { Suspense } from "react";
import { notFound } from "next/navigation";
import { Protected } from "@/components/Protected";
import { StatusCard } from "@/components/StatusCard";
import { hasLocale } from "@/i18n/config";
import { format, getDictionary } from "@/i18n/dictionaries";

export default async function Home({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <Suspense fallback={<p className="muted loading">{d.common.loading}</p>}>
      <Protected lang={lang}>
        {(p, dict) => (
          <>
            <h1>{format(dict.home.welcome, { name: p.display_name })}</h1>
            <p className="muted">
              {dict.home.fpo}: <strong>{p.fpo_name ?? dict.shell.noFpo}</strong> · {dict.home.role}:{" "}
              <strong>{(dict.shell.roles as Record<string, string>)[p.role]}</strong>
            </p>
            <h2>{dict.home.canDo}</h2>
            <ul>
              {p.capabilities.map((c) => (
                <li key={c}>{(dict.home.capabilities as Record<string, string>)[c] ?? c}</li>
              ))}
            </ul>
            <p className="muted">{dict.home.soon}</p>
            <Suspense fallback={<p className="muted">{dict.status.checking}</p>}>
              <StatusCard d={dict} />
            </Suspense>
          </>
        )}
      </Protected>
    </Suspense>
  );
}
