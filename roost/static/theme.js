"use strict";

(() => {
  const key = "roost-theme", accentKey = "roost-accent", textKey = "roost-text-size";
  const accents = ["forest", "ocean", "violet", "rose", "amber", "slate"];
  const textSizes = ["small", "default", "large", "larger"];
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  let choice;
  try {
    const saved = localStorage.getItem(key);
    if (saved === "light" || saved === "dark") choice = saved;
    // The accent is a synced setting; this copy only avoids a flash of the default colour.
    const accent = localStorage.getItem(accentKey);
    if (accents.includes(accent)) document.documentElement.dataset.accent = accent;
    const textSize = localStorage.getItem(textKey);
    if (textSizes.includes(textSize)) document.documentElement.dataset.textSize = textSize;
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
    toggle() { this.set(current() === "dark" ? "light" : "dark"); },
    // Text size is this device's own choice, like the theme: a phone and a desktop often want different sizes.
    textSize() { return textSizes.includes(document.documentElement.dataset.textSize) ? document.documentElement.dataset.textSize : "default"; },
    setTextSize(name) {
      const size = textSizes.includes(name) ? name : "default";
      document.documentElement.dataset.textSize = size;
      remember(textKey, size === "default" ? undefined : size);
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
