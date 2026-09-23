// Theme store: light/dark, persisted to localStorage, applied as a `dark`
// class on <html> (matches the `@custom-variant dark (&:is(.dark *))` in
// index.css). A blocking inline script in index.html applies the initial
// class before paint so there's no flash of the wrong theme; this module
// just needs to agree with that script on the storage key and fallback.

import { useSyncExternalStore } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "husk-theme";

function systemPreference(): Theme {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function readStored(): Theme | null {
  const v = localStorage.getItem(STORAGE_KEY);
  return v === "light" || v === "dark" ? v : null;
}

function apply(theme: Theme) {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

let current: Theme = readStored() ?? systemPreference();

const listeners = new Set<() => void>();

export function getTheme(): Theme {
  return current;
}

export function setTheme(theme: Theme) {
  if (theme === current) return;
  current = theme;
  localStorage.setItem(STORAGE_KEY, theme);
  apply(theme);
  listeners.forEach((l) => l());
}

export function toggleTheme() {
  setTheme(current === "dark" ? "light" : "dark");
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function useTheme(): [Theme, () => void] {
  const theme = useSyncExternalStore(subscribe, getTheme, getTheme);
  return [theme, toggleTheme];
}
