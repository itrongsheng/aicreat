// 明暗主题：dark 时给 html 加 dark class（Element Plus 暗色 CSS 变量），持久化到 localStorage
import { defineStore } from "pinia";

export type ThemeMode = "light" | "dark";
const STORAGE_KEY = "aicreat.theme";

function initialMode(): ThemeMode {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    /* ignore */
  }
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export const useThemeStore = defineStore("theme", {
  state: () => ({ mode: initialMode() as ThemeMode }),
  getters: {
    isDark: (s): boolean => s.mode === "dark",
  },
  actions: {
    apply() {
      document.documentElement.classList.toggle("dark", this.mode === "dark");
      document.documentElement.style.colorScheme = this.mode;
    },
    setMode(mode: ThemeMode) {
      this.mode = mode;
      try {
        localStorage.setItem(STORAGE_KEY, mode);
      } catch {
        /* ignore */
      }
      this.apply();
    },
    toggle() {
      this.setMode(this.mode === "dark" ? "light" : "dark");
    },
  },
});
