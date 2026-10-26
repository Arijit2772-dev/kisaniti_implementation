import type { ReactNode } from "react";
import { ShellLanguage } from "@/components/ShellLanguage";
import { SignOutButton } from "@/components/SignOutButton";
import type { Locale } from "@/i18n/config";
import type { Dictionary } from "@/i18n/dictionaries";
import type { Principal } from "@/lib/session";

/** The frame around every signed-in page: who and which FPO, language, navigation, sign out. */
export function Shell({ lang, p, d, limited, children }: { lang: Locale; p: Principal; d: Dictionary; limited: boolean; children: ReactNode }) {
  const roles = d.shell.roles as Record<string, string>;
  return (
    <div className="shell">
      <header className="shell-bar">
        <div className="shell-id">
          <a className="brand" href={`/${lang}`}>
            {d.app.name}
          </a>
          <span className="muted who">
            <strong>{p.fpo_name ?? d.shell.noFpo}</strong> · {p.display_name} · {roles[p.role] ?? p.role}
          </span>
        </div>
        <div className="shell-actions">
          <ShellLanguage lang={lang} csrf={p.csrf_token} label={d.language.switchTo} />
          <SignOutButton lang={lang} csrf={p.csrf_token} label={d.shell.signOut} errors={d.errors} />
        </div>
      </header>
      {!limited && (
        <nav className="shell-nav" aria-label={d.shell.menu}>
          <a href={`/${lang}`}>{d.shell.home}</a>
          {p.capabilities.includes("data.read") && (
            <>
              <a href={`/${lang}/members`}>{d.shell.members}</a>
              <a href={`/${lang}/lots`}>{d.shell.lots}</a>
              <a href={`/${lang}/prices`}>{d.shell.prices}</a>
              <a href={`/${lang}/plans`}>{d.shell.plans}</a>
              <a href={`/${lang}/contacts`}>{d.shell.contacts}</a>
              <a href={`/${lang}/messages`}>{d.shell.messages}</a>
              <a href={`/${lang}/sales`}>{d.shell.sales}</a>
              <a href={`/${lang}/devices`}>{d.shell.devices}</a>
            </>
          )}
          {p.capabilities.includes("users.manage") && <a href={`/${lang}/users`}>{d.shell.users}</a>}
          <a href={`/${lang}/status`}>{d.shell.status}</a>
        </nav>
      )}
      <main>{children}</main>
    </div>
  );
}
