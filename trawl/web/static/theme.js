// Runs in <head> before first paint: picks the theme (?theme=, saved choice, then OS) so there is no flash.
(function () {
  var root = document.documentElement;
  var fromUrl = new URLSearchParams(location.search).get("theme");
  var saved = null;
  try { saved = localStorage.getItem("trawl-theme"); } catch (e) { /* storage blocked */ }
  var theme = fromUrl === "light" || fromUrl === "dark" ? fromUrl : saved;
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  root.setAttribute("data-theme", theme);

  window.trawlToggleTheme = function () {
    var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("trawl-theme", next); } catch (e) { /* ignore */ }
    document.dispatchEvent(new CustomEvent("themechange", { detail: next }));
  };
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("themeToggle");
    if (btn) btn.addEventListener("click", window.trawlToggleTheme);
  });
})();
