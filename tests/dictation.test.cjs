const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { setImmediate: nextTurn } = require("node:timers/promises");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../app/static/js/dictation.js"), "utf8");

class Control extends EventTarget {
  value = "";
  textContent = "";
  hidden = false;
  pause() {}
  load() {}
  removeAttribute() {}
  focus() {}
  toggleAttribute() {}
  click() { this.dispatchEvent(new Event("click")); }
}

function setup(getUserMedia) {
  const controls = new Map();
  function select(selector) {
    if (!controls.has(selector)) controls.set(selector, new Control());
    return controls.get(selector);
  }
  const form = new Control();
  const naturalText = new Control();
  naturalText.value = "Keep the teacher’s instructions.";
  form.elements = { natural_text: naturalText, source_transcript: new Control() };
  form.querySelector = select;
  const dictation = select("[data-dictation]");
  dictation.closest = () => form;
  dictation.querySelector = select;
  const MediaRecorder = { isTypeSupported: () => true };
  runInNewContext(script, {
    document: { querySelector: select },
    window: Object.assign(new EventTarget(), { MediaRecorder }),
    navigator: { mediaDevices: getUserMedia ? { getUserMedia } : undefined },
    AbortController, clearInterval, clearTimeout,
    fetch: () => assert.fail("Audio should stay on the device"),
  });
  return { naturalText, start: select("[data-record]"), cancel: select("[data-cancel]"),
    status: select("[data-record-status]") };
}

test("denied permission leaves the teacher’s text available and identifies recovery", async () => {
  let attempts = 0;
  const { start, status, naturalText } = setup(async () => {
    attempts++;
    throw new DOMException("Denied", "NotAllowedError");
  });
  assert.equal(attempts, 0);
  start.click();
  await nextTurn();
  assert.equal(attempts, 1);
  assert.match(status.textContent, /permission was denied.*browser settings or keep typing/);
  assert.equal(naturalText.value, "Keep the teacher’s instructions.");
});

test("cancelling while permission is pending releases any stream that arrives later", async () => {
  let grantPermission;
  let stopped = false;
  const { start, cancel, status, naturalText } = setup(() => new Promise(resolve => {
    grantPermission = resolve;
  }));
  start.click();
  cancel.click();
  grantPermission({ getTracks: () => [{ stop: () => { stopped = true; } }] });
  await nextTurn();
  assert.equal(stopped, true);
  assert.match(status.textContent, /cancelled/);
  assert.equal(naturalText.value, "Keep the teacher’s instructions.");
});

test("a browser without recording support leaves text entry available", () => {
  const { status, naturalText } = setup();
  assert.match(status.textContent, /Recording is unavailable.*type the assignment/);
  assert.equal(naturalText.value, "Keep the teacher’s instructions.");
});
