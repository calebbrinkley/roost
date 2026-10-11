"use strict";

(() => {
  const key = "roost-theme";
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  let choice;
  try {
    const saved = localStorage.getItem(key);
    if (saved === "light" || saved === "dark") choice = saved;
  } catch { /* Keep the theme usable when storage is unavailable. */ }

  function current() { return choice || (systemDark.matches ? "dark" : "light"); }
  function apply() {
    const theme = current();
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#15181d" : "#f7f8fa";
    window.dispatchEvent(new Event("roostthemechange"));
  }

  window.roostTheme = {
    current,
    toggle() {
      choice = current() === "dark" ? "light" : "dark";
      try { localStorage.setItem(key, choice); } catch { /* The current page still updates. */ }
      apply();
    },
  };
  systemDark.addEventListener("change", () => { if (!choice) apply(); });
  apply();
})();
