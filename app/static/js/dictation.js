const dictation = document.querySelector("[data-dictation]");

if (dictation) {
  const form = dictation.closest("form");
  const input = form.elements.natural_text;
  const review = form.querySelector("[data-save-assignment]");
  const start = dictation.querySelector("[data-record]");
  const stop = dictation.querySelector("[data-stop]");
  const cancel = dictation.querySelector("[data-cancel]");
  const status = dictation.querySelector("[data-record-status]");
  const format = window.MediaRecorder && [
    "audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus",
  ].find(type => MediaRecorder.isTypeSupported(type));
  const limit = 5 * 1024 * 1024;
  let current;

  function stopMicrophone(session) {
    clearTimeout(session.timer);
    session.stream?.getTracks().forEach(track => track.stop());
  }

  function show(state, message) {
    start.hidden = state !== "idle";
    stop.hidden = state !== "recording";
    cancel.hidden = state === "idle";
    cancel.textContent = state === "transcribing" ? "Cancel transcription" : "Cancel recording";
    review.disabled = state !== "idle";
    status.textContent = message;
  }

  function finish(session, message, focus = start) {
    if (current !== session) return;
    current = undefined;
    session.controller.abort();
    if (session.recorder?.state === "recording") session.recorder.stop();
    stopMicrophone(session);
    session.chunks.length = 0;
    show("idle", message);
    focus?.focus();
  }

  async function transcribe(session) {
    if (current !== session) return;
    stopMicrophone(session);
    const audio = new Blob(session.chunks, { type: session.recorder.mimeType || format });
    session.chunks.length = 0;
    if (!audio.size) {
      finish(session, "No audio was recorded. Try again or type the assignment.");
      return;
    }
    show("transcribing", "Transcribing recording…");
    cancel.focus();
    try {
      const response = await fetch(dictation.dataset.endpoint, {
        method: "POST",
        headers: { "X-CSRF-Token": form.elements.csrf_token.value, Accept: "application/json" },
        body: audio,
        signal: session.controller.signal,
      });
      const result = await response.json();
      if (current !== session) return;
      if (!response.ok || typeof result.text !== "string") {
        const message = typeof result.detail === "string" ? result.detail : "Transcription failed.";
        finish(session, message + " Your text is still here.");
        return;
      }
      const separator = input.value ? "\n" : "";
      if (input.value.length + separator.length + result.text.length > input.maxLength) {
        finish(session, "There’s no room to add the transcript. Shorten the text and try again.");
        return;
      }
      input.value += separator + result.text;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      finish(session, "Transcript added. Check the class, names, numbers, and deadline before reviewing the draft.", input);
    } catch {
      finish(session, "Transcription failed. Your text is still here. Try again or keep typing.");
    }
  }

  dictation.hidden = false;
  if (!navigator.mediaDevices?.getUserMedia || !format) {
    start.hidden = true;
    dictation.querySelector("#dictation-privacy").hidden = true;
    status.textContent = "This browser can’t record here. Type the assignment in the field above.";
  } else {
    start.addEventListener("click", async () => {
      const session = { controller: new AbortController(), chunks: [], size: 0 };
      current = session;
      show("permission", "Waiting for microphone permission…");
      cancel.focus();
      try {
        session.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (current !== session) {
          stopMicrophone(session);
          return;
        }
        const recorder = new MediaRecorder(session.stream, { mimeType: format });
        session.recorder = recorder;
        recorder.addEventListener("dataavailable", event => {
          if (current !== session || !event.data.size) return;
          session.size += event.data.size;
          if (session.size > limit) {
            finish(session, "Recording was too large and was discarded. Try a shorter recording.");
            return;
          }
          session.chunks.push(event.data);
        });
        recorder.addEventListener("stop", () => transcribe(session), { once: true });
        recorder.addEventListener("error", () => {
          finish(session, "The microphone stopped working. Try again or keep typing.");
        }, { once: true });
        recorder.start(500);
        show("recording", "Recording… Stop and transcribe when you’re ready.");
        stop.focus();
        session.timer = setTimeout(() => {
          finish(session, "The 60-second limit was reached. Audio was discarded. Try a shorter recording.");
        }, 60000);
      } catch (error) {
        finish(session, error.name === "NotAllowedError"
          ? "Microphone permission was denied. Allow it in your browser or keep typing."
          : "The microphone couldn’t start. Check your microphone or keep typing.");
      }
    });
    stop.addEventListener("click", () => {
      if (current?.recorder.state !== "recording") return;
      current.recorder.stop();
      stopMicrophone(current);
      show("transcribing", "Transcribing recording…");
      cancel.focus();
    });
    cancel.addEventListener("click", () => {
      const transcribing = cancel.textContent === "Cancel transcription";
      finish(current, transcribing
        ? "Transcription cancelled. Your text is unchanged."
        : "Recording discarded. Your text is unchanged.");
    });
    window.addEventListener("pagehide", () => {
      if (current) finish(current, "Recording ended.", null);
    });
  }
}
