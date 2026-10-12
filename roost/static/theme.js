"use strict";

(() => {
  const key = "roost-theme", accentKey = "roost-accent";
  const accents = ["forest", "ocean", "violet", "rose", "amber", "slate"];
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  let choice;
  try {
    const saved = localStorage.getItem(key);
    if (saved === "light" || saved === "dark") choice = saved;
    // The accent is a synced setting; this copy only avoids a flash of the default colour.
    const accent = localStorage.getItem(accentKey);
    if (accents.includes(accent)) document.documentElement.dataset.accent = accent;
  } catch { /* Keep the theme usable when storage is unavailable. */ }

  function current() { return choice || (systemDark.matches ? "dark" : "light"); }
  function apply() {
    const theme = current();
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#15181d" : "#ffffff";
    window.dispatchEvent(new Event("roostthemechange"));
  }

  function remember(name, value) {
    try { value ? localStorage.setItem(name, value) : localStorage.removeItem(name); } catch { /* The current page still updates. */ }
  }

  window.roostTheme = {
    current,
    // "system" follows the device; "light" or "dark" is this device's own choice.
    choice() { return choice || "system"; },
    set(mode) {
      choice = mode === "light" || mode === "dark" ? mode : undefined;
      remember(key, choice);
      apply();
    },
    setAccent(name) {
      const accent = accents.includes(name) ? name : accents[0];
      document.documentElement.dataset.accent = accent;
      remember(accentKey, accent);
    },
  };
  systemDark.addEventListener("change", () => { if (!choice) apply(); });
  apply();
})();
