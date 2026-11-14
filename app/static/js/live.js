(() => {
  const notice = document.querySelector("[data-live-workspace]");
  const helpPanel = document.querySelector("[data-teacher-help-cursor]");
  const workspace = notice?.dataset.liveWorkspace ?? "teacher";
  const helpKey = helpPanel ? `class-relay-help:${helpPanel.dataset.teacherId}` : null;
  const announcement = document.querySelector("[data-help-announcement]");
  let helpCursor = Number(helpPanel?.dataset.teacherHelpCursor ?? 0);
  let currentHelp = null;
  if (helpKey) {
    try {
      const saved = sessionStorage.getItem(helpKey);
      if (saved !== null && /^\d+$/.test(saved)) helpCursor = Number(saved);
      sessionStorage.setItem(helpKey, helpCursor);
    } catch {
      // Keep the cursor for the current page when storage is unavailable.
    }
  }
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
      form.hasAttribute("data-dictation-active") ||
      (form.contains(focused) && focused.matches("input, select, textarea"))
    );
  }

  function showHelp(requests) {
    const next = requests.find(item => item.id > helpCursor);
    if (!next) {
      currentHelp = null;
      helpPanel.hidden = true;
      announcement.textContent = "";
      return;
    }
    if (currentHelp?.id === next.id && currentHelp.message === next.message) return;
    const newRequest = currentHelp?.id !== next.id;
    currentHelp = next;
    for (const [selector, value] of Object.entries({
      student: next.student, class: `${next.classroom} · ${next.school}`,
      assignment: next.assignment, requested: next.requested_at,
      due: next.due_at, message: next.message,
    })) {
      helpPanel.querySelector(`[data-help-${selector}]`).textContent = value;
    }
    helpPanel.querySelector("[data-help-open]").href = next.url;
    if (newRequest) helpPanel.querySelector(".teacher-help-details").scrollTop = 0;
    helpPanel.hidden = false;
    announcement.textContent = newRequest
      ? `${next.student} requested help with ${next.assignment}.`
      : `${next.student} added details to the help request.`;
  }

  function dismissHelp() {
    if (!currentHelp) return;
    helpCursor = currentHelp.id;
    showHelp([]);
    try {
      sessionStorage.setItem(helpKey, helpCursor);
    } catch {
      // The current page still remembers the dismissal.
    }
  }

  async function check() {
    clearTimeout(timer);
    if (checking || submitting || stopped || document.hidden) return;
    checking = true;
    try {
      const query = helpPanel ? `?help_after=${helpCursor}` : "";
      const result = await fetch(`/api/live/${workspace}${query}`, {cache: "no-store"});
      if (result.status === 401 || result.status === 403) {
        stopped = true;
        location.replace("/");
        return;
      }
      if (!result.ok) return;
      const {version, help_requests: helpRequests = []} = await result.json();
      if (submitting || document.hidden) return;
      if (helpPanel) showHelp(helpRequests);
      if (!notice) return;
      if (version === notice.dataset.liveVersion) {
        notice.hidden = true;
        return;
      }
      if (currentHelp) return;
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

  helpPanel?.querySelector("[data-help-dismiss]").addEventListener("click", () => {
    dismissHelp();
    check();
  });
  helpPanel?.querySelector("[data-help-open]").addEventListener("click", event => {
    if (event.defaultPrevented) return;
    dismissHelp();
  });
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
