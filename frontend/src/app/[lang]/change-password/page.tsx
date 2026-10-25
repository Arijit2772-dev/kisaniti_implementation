import { Suspense } from "react";
import { notFound } from "next/navigation";
import { ChangePasswordForm } from "@/components/ChangePasswordForm";
import { Protected } from "@/components/Protected";
import { hasLocale } from "@/i18n/config";
import { getDictionary } from "@/i18n/dictionaries";

export default async function ChangePasswordPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <Suspense fallback={<p className="muted loading">{d.common.loading}</p>}>
      <Protected lang={lang} allowPendingPassword>
        {(p, dict) => (
          <>
            <h1>{dict.changePassword.title}</h1>
            {p.must_change_password && <p className="banner">{dict.changePassword.intro}</p>}
            <ChangePasswordForm lang={lang} csrf={p.csrf_token} t={dict.changePassword} errors={dict.errors} />
          </>
        )}
      </Protected>
    </Suspense>
  );
}
