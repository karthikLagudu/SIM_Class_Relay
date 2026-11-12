const demoToggle = document.getElementById("demo-toggle");
const demoPanel = document.getElementById("demo-accounts");
const signInForm = document.getElementById("login-form");
const demoStatus = document.getElementById("demo-login-status");
const demoSearch = document.getElementById("demo-search");
const demoResultCount = document.getElementById("demo-result-count");
const demoEmpty = document.getElementById("demo-empty");
const demoRoleFilters = [...demoPanel.querySelectorAll("[data-demo-role]")];
let activeDemoRole = demoRoleFilters[0];

function demoRoleResults(filter) {
  return document.getElementById(filter.getAttribute("aria-controls"));
}

function updateDemoResults() {
  const terms = demoSearch.value.toLowerCase().match(/\S+/g) ?? [];
  const accounts = demoRoleResults(activeDemoRole).querySelectorAll("[data-demo-search]");
  let matching = 0;

  for (const account of accounts) {
    const searchable = account.dataset.demoSearch.toLowerCase();
    const matches = terms.every((term) => searchable.includes(term));
    account.hidden = !matches;
    if (matches) matching += 1;
  }

  demoResultCount.textContent = `${matching} matching ${matching === 1 ? "account" : "accounts"}`;
  demoEmpty.hidden = matching !== 0;
}

function selectDemoRole(selected) {
  activeDemoRole = selected;
  for (const filter of demoRoleFilters) {
    const isActive = filter === selected;
    filter.setAttribute("aria-pressed", String(isActive));
    demoRoleResults(filter).hidden = !isActive;
  }
  updateDemoResults();
}

demoSearch.addEventListener("input", updateDemoResults);

demoToggle.addEventListener("click", () => {
  const isOpening = demoPanel.hidden;
  demoPanel.hidden = !isOpening;
  demoToggle.setAttribute("aria-expanded", String(isOpening));
  demoToggle.textContent = isOpening ? "Hide demo accounts" : "Show demo accounts";
  if (isOpening) selectDemoRole(demoRoleFilters[0]);
});

demoPanel.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.hasAttribute("data-demo-role")) {
    selectDemoRole(button);
    return;
  }
  if (button.hasAttribute("data-demo-clear")) {
    demoSearch.value = "";
    updateDemoResults();
    demoSearch.focus();
    return;
  }
  if (!button.hasAttribute("data-demo-email")) return;

  signInForm.elements.namedItem("email").value = button.dataset.demoEmail;
  signInForm.elements.namedItem("password").value = button.dataset.demoPassword;
  demoStatus.hidden = false;
  demoStatus.textContent = `Demo credentials filled for ${button.dataset.demoName}. Press Sign in to continue.`;
  signInForm.querySelector('button[type="submit"]').focus();
});
