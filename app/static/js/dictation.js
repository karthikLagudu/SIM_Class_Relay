const dictation = document.querySelector("[data-dictation]");

if (dictation) {
  const form = dictation.closest("form");
  const input = form.elements.natural_text;
  const original = form.elements.source_transcript;
  const draft = form.querySelector("[data-save-assignment]");
  const start = form.querySelector("[data-record]");
  const control = name => dictation.querySelector(`[data-${name}]`);
  const stop = control("stop");
  const play = control("play");
  const recordAgain = control("record-again");
  const send = control("transcribe");
  const cancel = control("cancel");
  const status = control("record-status");
  const clock = control("recording-clock");
  const elapsed = control("elapsed");
  const player = control("recording-audio");
  const transcript = control("transcript");
  const append = control("append-transcript");
  const replace = control("replace-transcript");
  const format = [
    "audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus",
  ].find(type => window.MediaRecorder?.isTypeSupported?.(type));
  const limit = 5 * 1024 * 1024;
  let current;
  let stage = "idle";

  function stopMicrophone(session) {
    clearInterval(session.clock);
    clearTimeout(session.timeout);
    session.stream?.getTracks().forEach(track => track.stop());
  }

  function releaseAudio(session) {
    if (session.url) URL.revokeObjectURL(session.url);
    player.pause();
    player.removeAttribute("src");
    player.load();
    session.audio = null;
    session.chunks.length = 0;
  }

  function updateTextActions() {
    const hasText = Boolean(input.value.trim());
    append.textContent = hasText ? "Append to instructions" : "Use transcript";
    replace.hidden = !hasText;
  }

  function show(next, message) {
    stage = next;
    form.toggleAttribute("data-dictation-active", next !== "idle");
    const reviewingAudio = next === "audio";
    const reviewingText = next === "transcript";
    start.hidden = next !== "idle";
    stop.hidden = next !== "recording";
    clock.hidden = next !== "recording";
    play.hidden = recordAgain.hidden = send.hidden = !reviewingAudio;
    cancel.hidden = next === "idle";
    control("record-actions").hidden = next === "idle";
    control("transcript-review").hidden = !reviewingText;
    draft.disabled = next !== "idle";
    status.textContent = message;
    if (reviewingText) updateTextActions();
  }

  function finish(session, message, focus = start) {
    if (current !== session) return;
    current = undefined;
    session.controller.abort();
    if (session.recorder?.state === "recording") session.recorder.stop();
    stopMicrophone(session);
    releaseAudio(session);
    transcript.value = "";
    show("idle", message);
    focus?.focus();
  }

  function stopRecording(session) {
    if (session?.recorder?.state !== "recording") return;
    session.recorder.stop();
    stopMicrophone(session);
    show("stopping", "Preparing your recording…");
    cancel.focus();
  }

  function preparePlayback(session) {
    if (current !== session) return;
    stopMicrophone(session);
    session.audio = new Blob(session.chunks, { type: session.recorder.mimeType || format });
    session.chunks.length = 0;
    if (!session.audio.size) {
      finish(session, "No audio was recorded. Try again and speak before pressing Stop, or keep typing.");
      return;
    }
    session.url = URL.createObjectURL(session.audio);
    player.src = session.url;
    show("audio", "Recording ready. Play it back or choose Transcribe.");
    play.focus();
  }

  const microphoneErrors = {
    NotAllowedError: "Microphone permission was denied. Allow access in your browser settings or keep typing.",
    NotFoundError: "No microphone was found. Plug one in and try again, or keep typing.",
    NotReadableError: "The microphone couldn’t be read. Check that it’s connected and available, or keep typing.",
  };

  async function startRecording() {
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
          finish(session, "Recording was too large and was discarded. Try a shorter recording or keep typing.");
          return;
        }
        session.chunks.push(event.data);
      });
      recorder.addEventListener("stop", () => preparePlayback(session), { once: true });
      recorder.addEventListener("error", () => {
        finish(session, "The microphone stopped working. Check it and try again, or keep typing.");
      }, { once: true });
      recorder.start(500);
      const started = performance.now();
      elapsed.textContent = "0:00";
      session.clock = setInterval(() => {
        const seconds = Math.min(60, Math.floor((performance.now() - started) / 1000));
        elapsed.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
      }, 200);
      session.timeout = setTimeout(() => stopRecording(session), 60000);
      show("recording", "Recording. Choose Stop when you’re ready.");
      stop.focus();
    } catch (error) {
      finish(session, microphoneErrors[error.name] || "The microphone couldn’t start. Check your audio settings or keep typing.");
    }
  }

  async function transcribe(session) {
    const audio = session.audio;
    releaseAudio(session);
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
      if (!response.ok || typeof result.text !== "string" || !result.text.trim()) {
        const message = typeof result.detail === "string"
          ? result.detail : "Transcription failed. Record again or keep typing.";
        finish(session, message + " Your instructions are unchanged.");
        return;
      }
      session.original = result.text;
      transcript.value = result.text;
      show("transcript", "Transcript ready to edit.");
      transcript.focus();
    } catch {
      finish(session, "Couldn’t reach transcription. Check your connection and record again, or keep typing. Your instructions are unchanged.");
    }
  }

  function joinText(first, second) {
    return [first, second].filter(text => text.trim()).join("\n");
  }

  function useTranscript(keepText) {
    const text = transcript.value.trim();
    if (!text) {
      status.textContent = "Enter transcript text, or choose Cancel.";
      transcript.focus();
      return;
    }
    const next = keepText ? joinText(input.value, text) : text;
    const source = keepText ? joinText(original.value, current.original) : current.original;
    if (next.length > input.maxLength || source.length > input.maxLength) {
      status.textContent = "These instructions exceed 5,000 characters. Shorten your text or use Replace instructions.";
      return;
    }
    input.value = next;
    original.value = source;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    finish(current, "Transcript added. Choose Create draft when the instructions are ready.", input);
  }

  dictation.hidden = false;
  if (!navigator.mediaDevices?.getUserMedia || !format) {
    dictation.querySelector("#dictation-privacy").hidden = true;
    status.textContent = "Recording is unavailable in this browser. Try a browser with microphone access or type the assignment.";
  } else {
    show("idle", "");
    start.addEventListener("click", startRecording);
    stop.addEventListener("click", () => stopRecording(current));
    recordAgain.addEventListener("click", () => {
      finish(current, "", null);
      startRecording();
    });
    play.addEventListener("click", () => {
      if (player.paused) {
        const session = current;
        player.play().catch(() => {
          if (current === session && stage === "audio") {
            status.textContent = "Playback failed. Try Record again or choose Transcribe.";
          }
        });
      } else {
        player.pause();
      }
    });
    player.addEventListener("playing", () => { play.textContent = "Pause recording"; });
    for (const event of ["pause", "ended", "emptied"]) {
      player.addEventListener(event, () => { play.textContent = "Play recording"; });
    }
    send.addEventListener("click", () => transcribe(current));
    cancel.addEventListener("click", () => finish(current, "Dictation cancelled. Your instructions are unchanged."));
    input.addEventListener("input", updateTextActions);
    append.addEventListener("click", () => useTranscript(true));
    replace.addEventListener("click", () => useTranscript(false));
    form.addEventListener("submit", event => {
      if (stage === "idle") return;
      event.preventDefault();
      status.textContent = "Finish or cancel dictation before creating the draft.";
    });
    window.addEventListener("pagehide", () => {
      if (current) finish(current, "Dictation ended.", null);
    });
  }
}
