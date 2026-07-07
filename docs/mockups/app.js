/* MyNestra mockup harness — theme, palette, sidebar collapse, preview switcher, nav treatment.
   Alpine/htmx will replace this in production; here it just makes the mockups interactive. */
(function () {
  "use strict";
  var root = document.documentElement;

  function systemTheme() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function currentTheme() {
    return root.getAttribute("data-theme") || systemTheme();
  }
  function setTheme(t) {
    root.setAttribute("data-theme", t);
    try { localStorage.setItem("mynestra-theme", t); } catch (e) {}
  }
  function setPalette(p) {
    root.setAttribute("data-palette", p);
    try { localStorage.setItem("mynestra-palette", p); } catch (e) {}
    syncSwatches();
  }
  function syncSwatches() {
    var p = root.getAttribute("data-palette") || "teal";
    var sws = document.querySelectorAll(".sw");
    for (var i = 0; i < sws.length; i++) sws[i].classList.toggle("active", sws[i].getAttribute("data-palette-set") === p);
  }

  // ---- restore saved prefs (and make theme explicit so palettes resolve in both modes) ----
  try {
    var savedTheme = localStorage.getItem("mynestra-theme");
    root.setAttribute("data-theme", savedTheme || systemTheme());
    var savedPalette = localStorage.getItem("mynestra-palette");
    root.setAttribute("data-palette", savedPalette || "teal");
  } catch (e) {
    if (!root.getAttribute("data-theme")) root.setAttribute("data-theme", systemTheme());
    if (!root.getAttribute("data-palette")) root.setAttribute("data-palette", "teal");
  }
  document.addEventListener("DOMContentLoaded", syncSwatches);

  // ---- delegated interactions ----
  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-action],[data-target],[data-nav-set],[data-tab],[data-palette-set]");
    if (!el) return;

    if (el.matches("[data-action='toggle-theme']")) {
      setTheme(currentTheme() === "dark" ? "light" : "dark");
      return;
    }
    if (el.hasAttribute("data-palette-set")) {
      setPalette(el.getAttribute("data-palette-set"));
      return;
    }
    if (el.matches("[data-action='toggle-sidebar']")) {
      var app = el.closest(".app");
      if (app) app.setAttribute("data-collapsed", app.getAttribute("data-collapsed") === "true" ? "false" : "true");
      return;
    }
    if (el.hasAttribute("data-target")) {
      var id = el.getAttribute("data-target");
      var tabs = document.querySelectorAll(".mk-tab");
      for (var i = 0; i < tabs.length; i++) tabs[i].classList.toggle("active", tabs[i] === el);
      var screens = document.querySelectorAll(".mk-screen");
      for (var j = 0; j < screens.length; j++) screens[j].classList.toggle("active", screens[j].id === id);
      window.scrollTo(0, 0);
      return;
    }
    if (el.hasAttribute("data-nav-set")) {
      var mode = el.getAttribute("data-nav-set");
      var apps = document.querySelectorAll(".app:not(.app--locked)");
      for (var k = 0; k < apps.length; k++) apps[k].setAttribute("data-nav", mode);
      var segs = el.parentNode.querySelectorAll("button");
      for (var s = 0; s < segs.length; s++) segs[s].classList.toggle("active", segs[s] === el);
      return;
    }
    if (el.hasAttribute("data-tab")) {
      var group = el.closest("[data-tabs]");
      if (!group) return;
      var name = el.getAttribute("data-tab");
      group.querySelectorAll("[data-tab]").forEach(function (t) { t.classList.toggle("active", t === el); });
      group.querySelectorAll("[data-panel]").forEach(function (p) {
        p.style.display = p.getAttribute("data-panel") === name ? "" : "none";
      });
      return;
    }
  });
})();
