// Runs in <head> before the stylesheet paints, so a visitor who chose the light theme never
// sees a flash of the dark one. A classic script on purpose: a module would run after paint.
try {
  if (localStorage.getItem("halo-theme") === "light") document.documentElement.dataset.theme = "light";
} catch {
  // Storage is blocked: the page stays on the dark theme.
}
