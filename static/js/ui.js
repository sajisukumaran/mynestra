// MyNestra UI behavior (Alpine). Presentation only — no business logic (DESIGN §7.5).
// The pre-paint script in <head> sets data-theme/data-palette before first paint; this store
// drives the interactive toggles afterwards.
document.addEventListener("alpine:init", () => {
  const root = document.documentElement;
  const save = (k, v) => {
    try { localStorage.setItem(k, v); } catch (e) { /* ignore */ }
  };
  // Persist the per-user theme server-side (DESIGN §7.2: theme is a User preference). Best-effort:
  // needs the CSRF cookie (set by any page with a form) + an authenticated session, else a no-op.
  const persistTheme = (theme) => {
    try {
      const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
      if (!m) return;
      fetch("/theme/", {
        method: "POST",
        headers: {
          "X-CSRFToken": decodeURIComponent(m[1]),
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: "theme=" + encodeURIComponent(theme),
        credentials: "same-origin",
      });
    } catch (e) { /* ignore */ }
  };
  Alpine.store("ui", {
    theme: root.getAttribute("data-theme") || "light",
    palette: root.getAttribute("data-palette") || "teal",
    collapsed: false,
    toggleTheme() {
      this.theme = this.theme === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", this.theme);
      save("mynestra-theme", this.theme);
      persistTheme(this.theme);
    },
    setPalette(p) {
      this.palette = p;
      root.setAttribute("data-palette", p);
      save("mynestra-palette", p);
    },
    toggleSidebar() {
      this.collapsed = !this.collapsed;
    },
  });
});
