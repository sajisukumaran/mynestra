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

  // Value-over-time chart hover overlay (opt-in via c-line-chart `points`). Presentation only:
  // tracks the nearest series point to the cursor to drive a tooltip/markers, and (when an
  // events_url is wired) htmx-loads that date's drill panel on click. Reads its data from the
  // element's data-* attributes, mirroring the window.__* JSON hand-off used elsewhere.
  Alpine.data("votChart", () => ({
    points: [],
    vbw: 640,
    eventsUrl: "",
    hovered: false,
    i: null,
    pct: 0,
    pt: { x: 0, ym: 0, yi: 0, iso: "", d: "", m: "", i: "", g: "" },
    init() {
      try {
        this.points = JSON.parse(this.$el.dataset.points || "[]");
      } catch (e) {
        this.points = [];
      }
      this.vbw = parseFloat(this.$el.dataset.vbw) || 640;
      this.eventsUrl = this.$el.dataset.eventsUrl || "";
    },
    move(e) {
      if (!this.points.length) return;
      const rect = this.$el.getBoundingClientRect();
      if (!rect.width) return;
      const vbx = ((e.clientX - rect.left) / rect.width) * this.vbw;
      // Nearest point by x (points are x-ascending) via binary search — O(log n), so this scales
      // to long daily-priced histories without a DOM node per point.
      let lo = 0;
      let hi = this.points.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (this.points[mid].x < vbx) lo = mid + 1;
        else hi = mid;
      }
      if (lo > 0 && Math.abs(this.points[lo - 1].x - vbx) <= Math.abs(this.points[lo].x - vbx)) {
        lo -= 1;
      }
      this.i = lo;
      this.pt = this.points[lo];
      this.pct = (this.pt.x / this.vbw) * 100;
      this.hovered = true;
    },
    hide() {
      this.hovered = false;
      this.i = null;
    },
    drill() {
      if (this.i === null || !this.eventsUrl || !window.htmx) return;
      const url = this.eventsUrl + "?on=" + encodeURIComponent(this.pt.iso);
      window.htmx.ajax("GET", url, { target: "#value-events" });
    },
  }));
});
