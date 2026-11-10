// The header button that switches between the light and dark themes and remembers the choice.

const PAGE = { light: "#f1ede7", dark: "#0a0a0a" };

// Dark is the page as written; light is the stored exception.
function current() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function show(button, theme) {
  if (theme === "light") document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
  button.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
  const colour = document.querySelector('meta[name="theme-color"]');
  if (colour) colour.setAttribute("content", PAGE[theme]);
}

export function bindThemeToggle() {
  const button = document.getElementById("theme-toggle");
  if (!button) return;
  show(button, current());
  button.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    show(button, next);
    try {
      localStorage.setItem("halo-theme", next);
    } catch {
      // Storage is blocked: the choice lasts for this page only.
    }
  });
}
