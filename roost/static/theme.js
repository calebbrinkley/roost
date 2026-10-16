"use strict";

(() => {
  const key = "roost-theme", accentKey = "roost-accent", textKey = "roost-text-size";
  const accents = ["forest", "ocean", "violet", "rose", "amber", "slate"];
  const textSizes = ["small", "default", "large", "larger"];
  const custom = /^#[0-9a-f]{6}$/;
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  let choice, customAccent;
  try {
    const saved = localStorage.getItem(key);
    if (saved === "light" || saved === "dark") choice = saved;
    // The accent is a synced setting; this copy only avoids a flash of the default colour.
    const accent = localStorage.getItem(accentKey);
    if (accents.includes(accent) || custom.test(accent)) setAccent(accent, false);
    const textSize = localStorage.getItem(textKey);
    if (textSizes.includes(textSize)) document.documentElement.dataset.textSize = textSize;
  } catch { /* Keep the theme usable when storage is unavailable. */ }

  function current() { return choice || (systemDark.matches ? "dark" : "light"); }
  function apply() {
    const theme = current();
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#15181d" : "#ffffff";
    paintCustomAccent();
    window.dispatchEvent(new Event("roostthemechange"));
  }

  function remember(name, value) {
    try { value ? localStorage.setItem(name, value) : localStorage.removeItem(name); } catch { /* The current page still updates. */ }
  }

  // A custom accent keeps the chosen colour for fills. Text on it is white or near-black, whichever reads better,
  // and accent-coloured text is the colour darkened (light theme) or lightened (dark) until it reads on the page.
  function rgb(hex) { return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)); }
  function luminance(colour) {
    const [r, g, b] = colour.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }
  function contrast(a, b) { const [x, y] = [luminance(a), luminance(b)].sort((m, n) => n - m); return (x + 0.05) / (y + 0.05); }
  function hex(colour) { return "#" + colour.map((v) => Math.round(v).toString(16).padStart(2, "0")).join(""); }
  function paintCustomAccent() {
    const style = document.documentElement.style;
    if (!customAccent) { for (const name of ["--accent", "--accent-ink", "--accent-text"]) style.removeProperty(name); return; }
    const colour = rgb(customAccent), dark = current() === "dark";
    const page = dark ? [21, 24, 29] : [255, 255, 255], toward = dark ? [255, 255, 255] : [0, 0, 0];
    let text = colour;
    for (let step = 1; step <= 20 && contrast(text, page) < 4.5; step++) text = colour.map((v, i) => v + (toward[i] - v) * step / 20);
    style.setProperty("--accent", customAccent);
    style.setProperty("--accent-ink", contrast(colour, [255, 255, 255]) >= contrast(colour, [20, 23, 28]) ? "#ffffff" : "#14171c");
    style.setProperty("--accent-text", hex(text));
  }
  // A preset name or a custom "#rrggbb"; anything else falls back to the first preset.
  function setAccent(name, save = true) {
    const accent = accents.includes(name) || custom.test(name) ? name : accents[0];
    customAccent = custom.test(accent) ? accent : undefined;
    document.documentElement.dataset.accent = customAccent ? "custom" : accent;
    paintCustomAccent();
    if (save) remember(accentKey, accent);
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
    // Text size is this device's own choice, like the theme: a phone and a desktop often want different sizes.
    textSize() { return textSizes.includes(document.documentElement.dataset.textSize) ? document.documentElement.dataset.textSize : "default"; },
    setTextSize(name) {
      const size = textSizes.includes(name) ? name : "default";
      document.documentElement.dataset.textSize = size;
      remember(textKey, size === "default" ? undefined : size);
    },
    setAccent,
  };
  systemDark.addEventListener("change", () => { if (!choice) apply(); });
  apply();
})();
