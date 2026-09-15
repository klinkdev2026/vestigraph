// Translation dictionary loader. Every user-visible string goes through t().
// Language is a non-sensitive preference and may live in localStorage.
const STORAGE_KEY = "vestigraph.language";
const SUPPORTED = ["zh-CN", "en"];
let current = "zh-CN";
let dictionaries = {};
const listeners = new Set();

export function supported() { return SUPPORTED.slice(); }
export function language() { return current; }

export function initialLanguage() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && SUPPORTED.includes(saved)) return saved;
  } catch (_) { /* storage may be unavailable */ }
  return "zh-CN";  // first launch defaults to Chinese (spec §11)
}

export async function load(lang) {
  if (!SUPPORTED.includes(lang)) lang = "zh-CN";
  if (!dictionaries[lang]) {
    try {
      const response = await fetch(`/static/locales/${lang}.json`, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`locale ${lang} unavailable`);
      dictionaries[lang] = await response.json();
    } catch (error) {
      const notice=document.getElementById("locale-load-error") || document.createElement("p");
      notice.id="locale-load-error";notice.setAttribute("role","alert");
      notice.textContent="Language file could not be loaded. Retry / Reload";
      const retry=document.createElement("button");retry.type="button";retry.textContent="Retry";
      retry.onclick=async()=>{await load(lang);applyTo(document);};notice.append(" ",retry);
      document.body.prepend(notice);
      console.error("Translation file unavailable",error);
    }
    if(dictionaries[lang])document.getElementById("locale-load-error")?.remove();
  }
  current = lang;
  try { localStorage.setItem(STORAGE_KEY, lang); } catch (_) { /* ignore */ }
  document.documentElement.lang = lang;
  for (const fn of listeners) fn(lang);
}

export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }

export function has(key) {
  const dict = dictionaries[current] || {};
  return Object.prototype.hasOwnProperty.call(dict, key);
}

/** t("key", {name: "x"}) -> string; missing keys return the key itself (visible, never silent). */
export function t(key, args) {
  const dict = dictionaries[current] || {};
  let text = Object.prototype.hasOwnProperty.call(dict, key) ? dict[key] : null;
  if (text === null) {
    const fallback = dictionaries["en"] || {};
    text = Object.prototype.hasOwnProperty.call(fallback, key) ? fallback[key] : key;
  }
  if (args) {
    for (const [name, value] of Object.entries(args)) {
      text = text.split(`{${name}}`).join(String(value));
    }
  }
  return text;
}

/** Apply data-i18n / data-i18n-title / data-i18n-aria / data-i18n-placeholder attributes. */
export function applyTo(root) {
  root.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  root.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  root.querySelectorAll("[data-i18n-aria]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.i18nAria)); });
  root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
}

// ---- formatting (display only; the server keeps UTC and ordering) ----------
export function formatTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  const text = new Intl.DateTimeFormat(current, {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date);
  return `${text} (${timeZoneLabel()})`;
}

export function formatRelative(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  const rtf = new Intl.RelativeTimeFormat(current, { numeric: "auto" });
  if (Math.abs(seconds) < 60) return rtf.format(-seconds, "second");
  if (Math.abs(seconds) < 3600) return rtf.format(-Math.round(seconds / 60), "minute");
  if (Math.abs(seconds) < 86400) return rtf.format(-Math.round(seconds / 3600), "hour");
  return rtf.format(-Math.round(seconds / 86400), "day");
}

export function timeZoneLabel() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (_) { return "UTC"; }
}

export function formatBytes(size) {
  if (typeof size !== "number" || !Number.isFinite(size) || size < 0) return "?";
  const units = ["B", "KiB", "MiB", "GiB"];
  let value = size, index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  const number = new Intl.NumberFormat(current, { maximumFractionDigits: index === 0 ? 0 : 1 }).format(value);
  return `${number} ${units[index]}`;
}

export function formatNumber(value) {
  if (typeof value !== "number") return "—";
  return new Intl.NumberFormat(current).format(value);
}
