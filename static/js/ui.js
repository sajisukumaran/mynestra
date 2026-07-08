// MyNestra UI behavior (Alpine). Presentation only — no business logic (DESIGN §7.5).
// The pre-paint script in <head> sets data-theme/data-palette before first paint; this store
// drives the interactive toggles afterwards.
document.addEventListener("alpine:init", () => {
  const root = document.documentElement;
  const save = (k, v) => {
    try { localStorage.setItem(k, v); } catch (e) { /* ignore */ }
  };
  Alpine.store("ui", {
    theme: root.getAttribute("data-theme") || "light",
    palette: root.getAttribute("data-palette") || "teal",
    collapsed: false,
    toggleTheme() {
      this.theme = this.theme === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", this.theme);
      save("mynestra-theme", this.theme);
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
