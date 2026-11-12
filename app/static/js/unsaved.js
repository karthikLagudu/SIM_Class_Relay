const dirtyForms = new Set();

function confirmLeaving(event) {
  event.preventDefault();
  event.returnValue = "";
}

function markDirty(form, dirty) {
  if (dirty) {
    dirtyForms.add(form);
    window.addEventListener("beforeunload", confirmLeaving);
  } else {
    dirtyForms.delete(form);
    if (!dirtyForms.size) window.removeEventListener("beforeunload", confirmLeaving);
  }
}

for (const form of document.querySelectorAll("[data-guard-unsaved]")) {
  const readFields = () => JSON.stringify([...new FormData(form)]);
  const original = readFields();
  const unsaved = form.hasAttribute("data-unsaved");
  const changed = () => markDirty(form, unsaved || readFields() !== original);
  changed();
  form.addEventListener("input", changed);
  form.addEventListener("change", changed);
  form.addEventListener("submit", () => markDirty(form, false));
}
