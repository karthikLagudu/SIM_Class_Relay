const formProgress = document.getElementById("form-progress");
const workspaceHeader = document.querySelector(".topbar");
let progressTimer;

new ResizeObserver(() => {
  document.documentElement.style.setProperty("--workspace-header-height", `${workspaceHeader.offsetHeight}px`);
}).observe(workspaceHeader);

document.addEventListener("submit", (event) => {
  if (event.defaultPrevented) return;
  const form = event.target;
  const message = form.id === "login-form" ? "Signing in…" : form.method === "post" ? "Saving…" : "Loading…";
  clearTimeout(progressTimer);
  progressTimer = setTimeout(() => {
    formProgress.textContent = message;
    formProgress.hidden = false;
  }, 200);
});

window.addEventListener("pageshow", () => {
  clearTimeout(progressTimer);
  formProgress.hidden = true;
});
