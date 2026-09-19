import { useSyncExternalStore } from "react";
import en from "../locales/en";
import es from "../locales/es";
import fr from "../locales/fr";
import de from "../locales/de";
import ja from "../locales/ja";
import ptBR from "../locales/pt-BR";

/** Every string the app can ask for. The English bundle is the source of
 *  truth, so a mistyped or missing key is a compile error here instead of a
 *  silent fallback to the raw key at runtime. */
export type MessageKey = keyof typeof en;

export const DEFAULT_LOCALE = "en";

/** localStorage entry holding the user's own pick. It outranks the server's
 *  config on purpose: a language chosen on this device has to survive every
 *  config save, and the picker below writes both places anyway. */
export const LOCALE_KEY = "mlo.locale";

/** The shipped locales, in the order the language picker lists them. Labels
 *  are endonyms — a reader looking for their own language finds it written in
 *  that language, never in the one they cannot read. */
export const LOCALES: { code: string; label: string }[] = [
  { code: "en", label: "English" },
  { code: "es", label: "Español" },
  { code: "fr", label: "Français" },
  { code: "de", label: "Deutsch" },
  { code: "ja", label: "日本語" },
  { code: "pt-BR", label: "Português (Brasil)" },
];

/** Partial: a locale may lag the English bundle (the type only promises the
 *  shape), and the runtime fallback below covers exactly that gap. */
const BUNDLES: Record<string, Partial<Record<MessageKey, string>>> = {
  en,
  es,
  fr,
  de,
  ja,
  "pt-BR": ptBR,
};

/** The shipped locale a tag asks for, or null when the app has no bundle for
 *  it — "zh-Hans" then falls through to English rather than to garbled
 *  half-translations. Tags are compared by their language subtag ("en-US" →
 *  "en", "zh-Hans-CN" → "zh"); code first at full length, so the one regional
 *  bundle (pt-BR) is not swallowed by a generic "pt" match. */
function supported(tag: string | null | undefined): string | null {
  if (!tag) return null;
  const t = tag.trim();
  if (!t) return null;
  const exact = LOCALES.find((l) => l.code.toLowerCase() === t.toLowerCase());
  if (exact) return exact.code;
  const lang = t.toLowerCase().split(/[-_]/)[0];
  return LOCALES.find((l) => l.code.toLowerCase().split(/[-_]/)[0] === lang)?.code ?? null;
}

/** The locale a config asks for, resolved the one documented way: this
 *  browser's own pick, then the server's `ui_locale`, then the browser's
 *  language, then English. `undefined` is the boot call, before any config has
 *  arrived — and it still answers with the right locale, so an early `t()`
 *  during the first render is never untranslated. */
export function localeFromConfig(cfg: { ui_locale?: string } | undefined): string {
  return (
    supported(readStoredPick()) ??
    supported(cfg?.ui_locale) ??
    supported(window.navigator.language) ??
    DEFAULT_LOCALE
  );
}

let current = localeFromConfig(undefined);
const listeners = new Set<() => void>();

/** The stored pick, or null.
 *
 *  Wrapped because this runs at module import (main.tsx initializes the locale
 *  before the first render): a browser with storage disabled (Firefox's
 *  `dom.storage.enabled=false`, some privacy modes) THROWS on mere access, and
 *  an unguarded read here would take the whole app down before it painted.
 *  Storing rarely fails, but a failure must cost the preference, not the click
 *  that set it. */
function readStoredPick(): string | null {
  try {
    return localStorage.getItem(LOCALE_KEY);
  } catch {
    return null;
  }
}

function apply(next: string, persist: boolean) {
  if (persist) {
    try {
      localStorage.setItem(LOCALE_KEY, next);
    } catch {
      /* the language still applies to this session */
    }
  }
  // `<html lang>` drives hyphenation, font fallback and the screen reader's
  // voice — it is set even when the value does not change, so the boot call
  // still fixes it up from index.html's default.
  document.documentElement.lang = next;
  if (next === current) return;
  current = next;
  for (const fn of listeners) fn();
}

/** Switch the app's language. Called from the picker: this is the user's own
 *  choice for this browser, so it is written to localStorage (see LOCALE_KEY). */
export function setLocale(code: string) {
  apply(supported(code) ?? DEFAULT_LOCALE, true);
}

/** Apply the locale the server config names — App and the settings page call
 *  this when /api/config resolves. Never written to localStorage: the stored
 *  value is the user's explicit pick, and a config value that arrived over the
 *  wire must not age into one (a later edit to `ui_locale` has to take effect
 *  in a browser that never picked anything). */
export function applyConfigLocale(cfg?: { ui_locale?: string }) {
  apply(localeFromConfig(cfg), false);
}

/** Translate a key: the current locale's string, else the English one, else
 *  the key itself (only reachable if a bundle was built against an older
 *  English file). `{name}` placeholders are filled from `vars`. */
export function t(key: MessageKey, vars?: Record<string, string | number>): string {
  const text = BUNDLES[current]?.[key] ?? (en as Record<string, string | undefined>)[key] ?? key;
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (m, name: string) => String(vars[name] ?? m));
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** The active locale code, re-rendering the caller when it changes. */
export function useLocale(): string {
  return useSyncExternalStore(subscribe, () => current);
}

/** The translator plus the current locale — what a component needs to render
 *  labels and to offer the switcher. */
export function useI18n() {
  const locale = useLocale();
  return { t, locale, setLocale, locales: LOCALES };
}
