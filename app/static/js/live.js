(() => {
  const notice = document.querySelector("[data-live-workspace]");
  const forms = new Map(
    [...document.querySelectorAll('#main-content form')]
      .map(form => [form, snapshot(form)])
  );
  let timer;
  let checking = false;
  let submitting = false;
  let stopped = false;

  function snapshot(form) {
    return JSON.stringify([...new FormData(form)].map(([name, value]) =>
      [name, value instanceof File ? [value.name, value.size, value.lastModified] : value]
    ));
  }

  function editing() {
    const focused = document.activeElement;
    return [...forms].some(([form, original]) =>
      form.hasAttribute("data-unsaved") || snapshot(form) !== original ||
      (form.contains(focused) && focused.matches("input, select, textarea"))
    );
  }

  async function check() {
    clearTimeout(timer);
    if (checking || submitting || stopped || document.hidden) return;
    checking = true;
    try {
      const result = await fetch(`/api/live/${notice.dataset.liveWorkspace}`, {cache: "no-store"});
      if (result.status === 401 || result.status === 403) {
        stopped = true;
        location.replace("/");
        return;
      }
      if (!result.ok) return;
      const {version} = await result.json();
      if (version === notice.dataset.liveVersion) {
        notice.hidden = true;
        return;
      }
      if (submitting || document.hidden) return;
      if (editing()) {
        notice.hidden = false;
        return;
      }
      stopped = true;
      location.reload();
    } catch {
      // The next check can recover from a temporary connection failure.
    } finally {
      checking = false;
      if (!stopped && !document.hidden && !submitting) timer = setTimeout(check, 3000);
    }
  }

  document.addEventListener("submit", event => {
    if (event.defaultPrevented) return;
    submitting = true;
    clearTimeout(timer);
  });
  document.addEventListener("visibilitychange", check);
  window.addEventListener("pageshow", event => {
    if (event.persisted) {
      submitting = false;
      stopped = false;
      check();
    }
  });
  timer = setTimeout(check, 3000);
})();
