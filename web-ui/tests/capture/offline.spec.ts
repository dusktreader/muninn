import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import { expect, test, type Page, type Route } from "@playwright/test";

import type { Capability, Conversation, SpeakerMapping, Transcript } from "../../src/api";

async function installToneMicrophone(page: Page): Promise<void> {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: async () => {
          const context = new AudioContext();
          await context.resume();
          const oscillator = context.createOscillator();
          const gain = context.createGain();
          const destination = context.createMediaStreamDestination();
          oscillator.frequency.value = 440;
          gain.gain.value = 0.2;
          oscillator.connect(gain).connect(destination);
          oscillator.start();
          destination.stream.getAudioTracks()[0].addEventListener("ended", () => void context.close(), { once: true });
          return destination.stream;
        },
      },
    });
  });
}

function installLocalAuth(page: Page, capabilities?: Capability[]): Promise<void> {
  return page.addInitScript((suppliedCapabilities) => {
    window.__MUNINN_AUTH__ = {
      capabilities: suppliedCapabilities ?? [
        "context:read",
        "metadata:write",
        "processing:admin",
        "deletion:write",
        "notes:human:create",
        "notes:human:edit",
        "notes:human:delete",
      ],
    };
  }, capabilities);
}

test.beforeEach(async ({ page }) => {
  await installLocalAuth(page);
});

function installUnauthenticatedState(page: Page): Promise<void> {
  return page.addInitScript(() => {
    Reflect.deleteProperty(window, "__MUNINN_AUTH__");
    sessionStorage.setItem("muninn.oidc.state", "pending-state");
    sessionStorage.setItem("muninn.oidc.verifier", "pending-verifier");
    sessionStorage.setItem("muninn.oidc.access_token", "session-access-token");
    localStorage.setItem("muninn.oidc.refresh_token", "local-refresh-token");
    localStorage.setItem("muninn.oidc.id_token", "local-id-token");
  });
}

async function expectNoAuthStorage(page: Page): Promise<void> {
  const authStorage = await page.evaluate(() => ({
    session: Object.keys(sessionStorage).filter((key) => key.startsWith("muninn.oidc.")),
    local: Object.keys(localStorage).filter((key) => key.startsWith("muninn.oidc.")),
  }));
  expect(authStorage).toEqual({ session: [], local: [] });
}

test("reload does not show the sign-in modal for a valid test session", async ({ page }) => {
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [] }));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in to continue" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Start recording" })).toBeVisible();

  await page.reload();

  await expect(page.getByRole("heading", { name: "Sign in to continue" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Start recording" })).toBeVisible();
});

test("sign-out is available on the sign-in screen and clears local authentication state", async ({ page }) => {
  await installUnauthenticatedState(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Sign in to continue" })).toBeVisible();
  const signOut = page.getByRole("button", { name: "Sign out", exact: true });
  await expect(signOut).toBeVisible();
  await page.getByRole("button", { name: "Sign in", exact: true }).focus();
  await page.keyboard.press("Tab");
  await expect(signOut).toBeFocused();
  await signOut.click();

  await expectNoAuthStorage(page);
  await expect(page.getByRole("heading", { name: "Sign in to continue" })).toBeVisible();
});

test("sign-out remains available after a sign-in error", async ({ page }) => {
  await installUnauthenticatedState(page);
  await page.goto("/callback?error=access_denied&state=pending-state");

  await expect(page.getByRole("alert")).toHaveText("access_denied");
  const signOut = page.getByRole("button", { name: "Sign out", exact: true });
  await expect(signOut).toBeVisible();
  await signOut.click();

  await expectNoAuthStorage(page);
  await expect(page.getByRole("heading", { name: "Sign in to continue" })).toBeVisible();
});

async function openDownloadSubmenu(page: Page) {
  await page.getByRole("menuitem", { name: "Download", exact: true }).click();
  return page.getByRole("menu", { name: "Download" });
}

function browserConversation(id: string, occurredAt: string, uploaded = false): Conversation {
  return {
    id,
    name: "Browser recording",
    description: null,
    occurred_at: occurredAt,
    people: [],
    tags: [],
    revision: 1,
    sync_state: uploaded ? "processing" : "local_only",
    recording_available: uploaded,
    transcript_available: false,
    summary_available: false,
  };
}

async function fulfillAudioStream(route: Route, data: Buffer, partialWithoutRange = false): Promise<void> {
  const range = route.request().headers().range;
  if (!range && !partialWithoutRange) {
    await route.fulfill({ status: 200, contentType: "audio/wav", body: data });
    return;
  }
  if (!range) {
    const bytes = data.subarray(0, 16_384);
    await route.fulfill({
      status: 206,
      contentType: "audio/wav",
      headers: { "Accept-Ranges": "bytes", "Content-Length": String(bytes.length), "Content-Range": `bytes 0-${bytes.length - 1}/${data.length}` },
      body: bytes,
    });
    return;
  }
  const match = range.match(/^bytes=(\d+)-(\d*)$/);
  if (!match) {
    await route.fulfill({ status: 416, headers: { "Content-Range": `bytes */${data.length}` } });
    return;
  }
  const start = Number(match[1]);
  const requestedEnd = match[2] ? Number(match[2]) : data.length - 1;
  const end = Math.min(requestedEnd, start + 16_383, data.length - 1);
  const bytes = data.subarray(start, end + 1);
  await route.fulfill({
    status: 206,
    contentType: "audio/wav",
    headers: {
      "Accept-Ranges": "bytes",
      "Content-Length": String(bytes.length),
      "Content-Range": `bytes ${start}-${end}/${data.length}`,
    },
    body: bytes,
  });
}

function waveFile(seconds = 10): Buffer {
  const sampleRate = 8000;
  const sampleCount = sampleRate * seconds;
  const buffer = Buffer.alloc(44 + sampleCount * 2);
  buffer.write("RIFF", 0);
  buffer.writeUInt32LE(buffer.length - 8, 4);
  buffer.write("WAVEfmt ", 8);
  buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20);
  buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(sampleRate, 24);
  buffer.writeUInt32LE(sampleRate * 2, 28);
  buffer.writeUInt16LE(2, 32);
  buffer.writeUInt16LE(16, 34);
  buffer.write("data", 36);
  buffer.writeUInt32LE(sampleCount * 2, 40);
  for (let index = 0; index < sampleCount; index += 1) {
    buffer.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 440 * index) / sampleRate) * 6000), 44 + index * 2);
  }
  return buffer;
}

async function openTranscriptReview(
  page: Page,
  conversation: Conversation,
  transcript: Transcript,
  speakerMapping: SpeakerMapping,
  audioHandler?: (route: Route) => Promise<void>,
): Promise<void> {
  let mapping = speakerMapping;
  await installLocalAuth(page, ["context:read", "metadata:write"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${conversation.id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${conversation.id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${conversation.id}/transcript`, (route) => route.fulfill({ json: transcript }));
  await page.route(`**/v1/conversations/${conversation.id}/speakers`, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: mapping });
      return;
    }
    const update = route.request().postDataJSON() as { speaker_number: number; display_name: string | null };
    mapping = {
      ...mapping,
      speaker_revision: mapping.speaker_revision + 1,
      speakers: mapping.speakers.map((speaker) => speaker.speaker_number === update.speaker_number
        ? { ...speaker, display_name: update.display_name }
        : speaker),
    };
    await route.fulfill({ status: 202, json: mapping });
  });
  await page.route(`**/v1/conversations/${conversation.id}/recording-access`, (route) => route.fulfill({ json: { grant: "transcript-test-grant" } }));
  await page.route(`**/v1/conversations/${conversation.id}/recording?grant=**`, audioHandler ?? ((route) => fulfillAudioStream(route, waveFile())));
  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  await page.getByRole("tab", { name: "Transcript" }).click();
}

async function openNoteComposerForScrubTest(page: Page, id: string) {
  const conversation = browserConversation(id, "2026-04-01T12:00:00Z", true);
  let releaseSave: () => void = () => undefined;
  let reportSave: (payload: { offset_seconds: number; text: string }) => void = () => undefined;
  const saveGate = new Promise<void>((resolve) => { releaseSave = resolve; });
  const saveRequest = new Promise<{ offset_seconds: number; text: string }>((resolve) => { reportSave = resolve; });
  await installLocalAuth(page);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}`, (route) => route.fulfill({ json: conversation }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/notes`, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: [] });
      return;
    }
    const payload = route.request().postDataJSON() as { offset_seconds: number; text: string };
    reportSave(payload);
    await saveGate;
    await route.fulfill({ status: 201, json: {
      id: `${id}-note`,
      conversation_id: id,
      offset_seconds: payload.offset_seconds,
      text: payload.text,
      subject: "local-development",
      author_kind: "human",
      delegated_session_id: null,
      created_at: "2026-04-01T12:00:01Z",
    } });
  });
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => route.fulfill({ json: { grant: `${id}-grant` } }));
  await page.route(`**/v1/conversations/${id}/recording?grant=**`, (route) => fulfillAudioStream(route, waveFile()));
  await page.goto(`/recordings/${id}`);
  const audio = page.getByTestId("recording-audio");
  const timeline = page.getByRole("slider", { name: "Recording timeline" });
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBe(10);
  await page.getByRole("button", { name: "Add note" }).click();
  return { audio, timeline, saveRequest, releaseSave };
}

test("capture uses the microphone without creating persistent browser storage", async ({ page }) => {
  let conversation: ReturnType<typeof browserConversation> | null = null;
  let uploaded = false;
  await installToneMicrophone(page);
  await page.route("**/v1/conversations", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: uploaded && conversation ? [browserConversation(conversation.id, conversation.occurred_at, true)] : [] });
      return;
    }
    const body = route.request().postDataJSON() as { id: string; occurred_at: string };
    conversation = browserConversation(body.id, body.occurred_at);
    await route.fulfill({ status: 201, json: conversation });
  });
  await page.route("**/v1/conversations/*/recording", async (route) => {
    uploaded = true;
    await route.fulfill({ status: 201, json: {} });
  });
  await page.route("**/v1/conversations/*/notes", (route) => route.fulfill({ json: [] }));
  await page.goto("/");
  await expect(page).toHaveURL(/\/recordings$/);

  const record = page.getByRole("button", { name: "Start recording" });
  await expect(record).toBeVisible();
  await record.click();
  await expect(page).toHaveURL(/\/recordings\/new$/);
  await expect(page.getByRole("button", { name: "Stop recording" })).toBeVisible();
  const waveform = page.getByRole("img", { name: "Live recording waveform" });
  await expect(waveform).toBeVisible();
  await expect.poll(() => waveform.evaluate((element) => {
    if (!(element instanceof HTMLCanvasElement)) return 0;
    const context = element.getContext("2d");
    if (!context) return 0;
    const pixels = context.getImageData(0, 0, element.width, element.height).data;
    let minY = element.height;
    let maxY = 0;
    for (let y = 0; y < element.height; y += 1) {
      for (let x = 0; x < element.width; x += 1) {
        if (pixels[(y * element.width + x) * 4 + 3] > 0) {
          minY = Math.min(minY, y);
          maxY = Math.max(maxY, y);
        }
      }
    }
    return maxY - minY;
  })).toBeGreaterThan(2);
  await page.getByRole("button", { name: "Stop recording" }).click();
  await expect(waveform).toHaveCount(0);
  await expect(page).toHaveURL(/\/recordings\/[0-9a-f-]+$/);

  const persistentState = await page.evaluate(async () => ({
    cacheNames: await caches.keys(),
    databases: await indexedDB.databases(),
    serviceWorkers: await navigator.serviceWorker.getRegistrations(),
  }));
  expect(persistentState.cacheNames).toEqual([]);
  expect(persistentState.databases).toEqual([]);
  expect(persistentState.serviceWorkers).toEqual([]);
});

test("recording details show playback while automatic upload is in progress", async ({ page }) => {
  let conversation: ReturnType<typeof browserConversation> | null = null;
  let uploaded = false;
  let recordingFetched = false;
  let rangeRequests = 0;
  const uploadGate: { finish?: () => void } = {};

  await installToneMicrophone(page);
  await page.route("**/v1/conversations", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: uploaded && conversation ? [browserConversation(conversation.id, conversation.occurred_at, true)] : [] });
      return;
    }
    const body = route.request().postDataJSON() as { id: string; occurred_at: string };
    conversation = browserConversation(body.id, body.occurred_at);
    await route.fulfill({ status: 201, json: conversation });
  });
  await page.route(/\/v1\/conversations\/[0-9a-f-]+$/, async (route) => {
    if (route.request().method() === "GET" && conversation) {
      await route.fulfill({ json: browserConversation(conversation.id, conversation.occurred_at, uploaded) });
      return;
    }
    await route.continue();
  });
  const persistedAudio = waveFile();
  await page.route("**/v1/conversations/*/recording-access", (route) => route.fulfill({ json: { grant: "test-grant" } }));
  await page.route("**/v1/conversations/*/recording?grant=**", async (route) => {
    recordingFetched = true;
    if (route.request().headers().range) rangeRequests += 1;
    await fulfillAudioStream(route, persistedAudio);
  });
  await page.route("**/v1/conversations/*/recording", async (route) => {
    await new Promise<void>((resolve) => { uploadGate.finish = resolve; });
    uploaded = true;
    await route.fulfill({ status: 201, json: {} });
  });
  await page.route("**/v1/conversations/*/notes", (route) => route.fulfill({ json: [] }));

  await page.goto("/");
  await page.getByRole("button", { name: "Start recording" }).click();
  await expect(page).toHaveURL(/\/recordings\/new$/);
  await page.waitForTimeout(400);
  await page.getByRole("button", { name: "Stop recording" }).click();
  await expect(page).toHaveURL(/\/recordings\/[0-9a-f-]+$/);
  await expect(page.getByRole("status").filter({ hasText: "Uploading" })).toHaveText("Uploading");
  await expect.poll(() => uploadGate.finish !== undefined).toBe(true);

  const preview = page.getByTestId("recording-audio");
  await expect(page.getByRole("button", { name: "Play recording" })).toBeVisible();
  await page.getByRole("button", { name: "Play recording" }).click();
  await expect(preview).toHaveAttribute("src", /^blob:/);
  await expect.poll(() => preview.evaluate((element) => (element as HTMLAudioElement).duration)).toBeGreaterThan(0);
  const previewTime = await preview.evaluate((element) => {
    const audio = element as HTMLAudioElement;
    audio.playbackRate = 0.1;
    audio.currentTime = Math.min(0.1, audio.duration / 2);
    audio.dispatchEvent(new Event("timeupdate"));
    return audio.currentTime;
  });
  const peakAmplitude = await preview.evaluate(async (element) => {
    if (!(element instanceof HTMLAudioElement)) return 0;
    const recording = await fetch(element.src).then((response) => response.arrayBuffer());
    const context = new AudioContext();
    try {
      const audio = await context.decodeAudioData(recording);
      const samples = audio.getChannelData(0);
      let peak = 0;
      for (const sample of samples) peak = Math.max(peak, Math.abs(sample));
      return peak;
    } finally {
      await context.close();
    }
  });
  expect(peakAmplitude).toBeGreaterThan(0.01);
  await expect(page.locator(".recording-player").getByRole("button", { name: "Export recording" })).toHaveCount(0);
  uploadGate.finish?.();
  await expect(page.getByRole("heading", { name: "Browser recording" })).toBeVisible();
  await expect(page.getByTestId("recording-audio")).toHaveAttribute("src", /\/v1\/conversations\/.+\/recording\?grant=test-grant/);
  await expect.poll(() => recordingFetched).toBe(true);
  await expect.poll(() => preview.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(previewTime - 0.05);
  await expect.poll(() => preview.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeLessThan(previewTime + 1);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  const detailUrl = page.url();

  await page.reload();
  await expect(page).toHaveURL(detailUrl);
  await expect(page.getByRole("heading", { name: "Browser recording" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Play recording" })).toBeVisible();
  await expect.poll(() => recordingFetched).toBe(true);
  expect(recordingFetched).toBe(true);
  expect(rangeRequests).toBeGreaterThan(0);
  const actionsTrigger = page.getByRole("button", { name: "Conversation actions" });
  await actionsTrigger.click();
  const downloads = await openDownloadSubmenu(page);
  await expect(downloads.getByRole("menuitem", { name: "Download recording" })).toBeEnabled();
  await actionsTrigger.click();
  await expect(page.getByTestId("recording-audio")).toHaveAttribute("src", /\/v1\/conversations\/.+\/recording\?grant=test-grant/);
});

test("paused capture playback preserves position when upload completes", async ({ page }) => {
  let conversation: ReturnType<typeof browserConversation> | null = null;
  let uploaded = false;
  const uploadGate: { finish?: () => void } = {};
  await installToneMicrophone(page);
  await page.route("**/v1/conversations", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: uploaded && conversation ? [browserConversation(conversation.id, conversation.occurred_at, true)] : [] });
      return;
    }
    const body = route.request().postDataJSON() as { id: string; occurred_at: string };
    conversation = browserConversation(body.id, body.occurred_at);
    await route.fulfill({ status: 201, json: conversation });
  });
  await page.route(/\/v1\/conversations\/[0-9a-f-]+$/, async (route) => {
    if (route.request().method() === "GET" && conversation) {
      await route.fulfill({ json: browserConversation(conversation.id, conversation.occurred_at, uploaded) });
      return;
    }
    await route.continue();
  });
  await page.route("**/v1/conversations/*/recording-access", (route) => route.fulfill({ json: { grant: "paused-grant" } }));
  await page.route("**/v1/conversations/*/recording?grant=**", (route) => fulfillAudioStream(route, waveFile()));
  await page.route("**/v1/conversations/*/recording", async (route) => {
    await new Promise<void>((resolve) => { uploadGate.finish = resolve; });
    uploaded = true;
    await route.fulfill({ status: 201, json: {} });
  });
  await page.route("**/v1/conversations/*/notes", (route) => route.fulfill({ json: [] }));

  await page.goto("/");
  await page.getByRole("button", { name: "Start recording" }).click();
  await page.waitForTimeout(400);
  await page.getByRole("button", { name: "Stop recording" }).click();
  await expect(page).toHaveURL(/\/recordings\/[0-9a-f-]+$/);
  await expect.poll(() => uploadGate.finish !== undefined).toBe(true);
  const audio = page.getByTestId("recording-audio");
  await page.getByRole("button", { name: "Play recording" }).click();
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBeGreaterThan(0);
  await audio.evaluate((element) => {
    const recording = element as HTMLAudioElement;
    recording.playbackRate = 0.1;
    recording.currentTime = Math.min(0.1, recording.duration / 2);
    recording.dispatchEvent(new Event("timeupdate"));
  });
  await page.getByRole("button", { name: "Pause recording" }).click();
  const pausedTime = await audio.evaluate((element) => (element as HTMLAudioElement).currentTime);
  uploadGate.finish?.();
  await expect(audio).toHaveAttribute("src", /\/v1\/conversations\/.+\/recording\?grant=paused-grant/);
  await expect(page.getByRole("button", { name: "Play recording" })).toBeVisible();
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(pausedTime - 0.05);
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeLessThan(pausedTime + 0.5);
});

test("uploaded recording streams through playback and timeline seeking", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000024";
  const conversation = browserConversation(id, "2026-04-01T12:00:00Z", true);
  let rangeRequests = 0;
  const note = {
    id: "00000000-0000-0000-0000-000000000025",
    conversation_id: id,
    offset_seconds: 4.25,
    text: "Marker note",
    subject: "local-development",
    author_kind: "human",
    delegated_session_id: null,
    created_at: "2026-04-01T12:00:01Z",
  };
  await installLocalAuth(page);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [note] }));
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => route.fulfill({ json: { grant: "test-grant" } }));
  await page.route(`**/v1/conversations/${id}/recording?grant=**`, async (route) => {
    const url = new URL(route.request().url());
    expect(url.searchParams.get("mode")).toBeNull();
    expect(route.request().headers().range).toBeDefined();
    rangeRequests += 1;
    await fulfillAudioStream(route, waveFile());
  });

  await page.goto(`/recordings/${id}`);
  const audio = page.getByTestId("recording-audio");
  const streamUrl = /\/v1\/conversations\/.+\/recording\?grant=test-grant/;
  await expect(audio).toHaveAttribute("src", streamUrl);
  await expect(page.locator(".recording-player")).toHaveAttribute("data-playback-source", "stream");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBe(10);
  expect(rangeRequests).toBeGreaterThan(0);

  const timeline = page.getByRole("slider", { name: "Recording timeline" });
  await expect(timeline).toBeEnabled();
  await timeline.focus();
  await timeline.press("End");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBe(10);
  await timeline.press("Home");
  await timeline.press("ArrowRight");
  await expect.poll(() => timeline.evaluate((element) => Number((element as HTMLInputElement).value))).toBeGreaterThan(0);
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThan(0);

  await page.getByRole("button", { name: "Play recording" }).click();
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  await page.getByRole("button", { name: "Jump to note at 00:04.25: Marker note" }).click();
  await expect(page.getByRole("tab", { name: "Notes" })).toHaveAttribute("aria-selected", "true");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(4.2);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  await page.locator(".note-text-button").click();
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(4.2);
  await expect(audio).toHaveAttribute("src", streamUrl);
  await expect(page.locator(".recording-player")).toHaveAttribute("data-playback-source", "stream");
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  expect(rangeRequests).toBeGreaterThan(0);
});

test("failed initial playback grant can be retried", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000036";
  const conversation = { ...browserConversation(id, "2026-04-01T12:00:00Z", true), transcript_available: true };
  const transcript: Transcript = {
    language: "en",
    segments: [{ evidence_id: "recoverable-segment", speaker: "Speaker 1", start_seconds: 2, end_seconds: 4, text: "Playback becomes available after retry." }],
    provider: "test",
    model: "test",
  };
  const speakers: SpeakerMapping = {
    speaker_revision: 1,
    speakers: [{ speaker_number: 1, label: "Speaker 1", display_name: null }],
    summary_job: null,
  };
  let grantRequests = 0;
  await installLocalAuth(page);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}`, (route) => route.fulfill({ json: conversation }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [{ id: "job-existing", kind: "transcription", state: "queued", attempts: 0, created_at: "2026-04-01T12:00:00Z" }] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({ json: transcript }));
  await page.route(`**/v1/conversations/${id}/speakers`, (route) => route.fulfill({ json: speakers }));
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => {
    grantRequests += 1;
    return grantRequests === 1
      ? route.fulfill({ status: 503, json: { error: "Unavailable" } })
      : route.fulfill({ json: { grant: "recovered-grant" } });
  });
  await page.route(`**/v1/conversations/${id}/recording?grant=**`, (route) => fulfillAudioStream(route, waveFile()));

  await page.goto(`/recordings/${id}`);
  await expect(page.locator('.player-unavailable[role="alert"]')).toContainText("Recording playback is unavailable.");
  await page.getByRole("tab", { name: "Transcript" }).click();
  const seekAction = page.getByRole("button", { name: "Play segment from 00:02.00" });
  const unavailableMessage = "Transcript playback is unavailable. Retry recording playback to enable segment seeking.";
  await expect(page.getByRole("status").filter({ hasText: unavailableMessage })).toBeVisible();
  await expect(seekAction).toBeDisabled();
  await expect(seekAction).toHaveAccessibleDescription(unavailableMessage);
  await page.locator(".transcript-segment p").click();
  await expect(page.getByTestId("recording-audio")).toHaveCount(0);

  await page.getByRole("button", { name: "Retry playback" }).click();
  const audio = page.getByTestId("recording-audio");
  await expect(audio).toHaveAttribute("src", /grant=recovered-grant/);
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBe(10);
  await expect(seekAction).toBeEnabled();
  await expect(page.getByRole("status").filter({ hasText: unavailableMessage })).toHaveCount(0);
  await page.locator(".transcript-segment p").click();
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(1.95);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  expect(grantRequests).toBe(2);
});

test("switching conversations resets playback state", async ({ page }) => {
  const firstId = "00000000-0000-0000-0000-000000000034";
  const secondId = "00000000-0000-0000-0000-000000000035";
  const conversations = [
    browserConversation(firstId, "2026-04-01T12:00:00Z", true),
    browserConversation(secondId, "2026-04-01T13:00:00Z", true),
  ];
  await installLocalAuth(page);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: conversations }));
  await page.route("**/v1/conversations/*/jobs", (route) => route.fulfill({ json: [] }));
  await page.route("**/v1/conversations/*/notes", (route) => route.fulfill({ json: [] }));
  await page.route("**/v1/conversations/*/recording-access", (route) => route.fulfill({ json: { grant: "test-grant" } }));
  await page.route("**/v1/conversations/*/recording?grant=**", (route) => fulfillAudioStream(route, waveFile()));

  await page.goto(`/recordings/${firstId}`);
  const audio = page.getByTestId("recording-audio");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBe(10);
  await audio.evaluate((element) => {
    const recording = element as HTMLAudioElement;
    recording.currentTime = 6;
    recording.dispatchEvent(new Event("timeupdate"));
  });
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThan(5.9);

  await page.getByRole("navigation", { name: "Breadcrumb" }).getByRole("link", { name: "Recordings" }).click();
  await page.getByRole("button", { name: "Open recording Browser recording" }).first().click();
  await expect(page).toHaveURL(new RegExp(`/recordings/${secondId}$`));
  await expect.poll(() => page.getByTestId("recording-audio").evaluate((element) => (element as HTMLAudioElement).duration)).toBe(10);
  await expect.poll(() => page.getByTestId("recording-audio").evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeLessThan(1);
});

test("expired playback grants refresh during playback and preserve the latest seek", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000026";
  const conversation = browserConversation(id, "2026-04-01T12:00:00Z", true);
  const streamedAudio = waveFile(600);
  const note = {
    id: "00000000-0000-0000-0000-000000000028",
    conversation_id: id,
    offset_seconds: 2.5,
    text: "Earlier marker",
    subject: "local-development",
    author_kind: "human",
    delegated_session_id: null,
    created_at: "2026-04-01T12:00:01Z",
  };
  let grantsIssued = 0;
  const expiredGrants = new Set<number>();
  const expiredRanges = new Map<number, number>();
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [note] }));
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => {
    grantsIssued += 1;
    return route.fulfill({ json: { grant: `test-grant-${grantsIssued}` } });
  });
  await page.route(`**/v1/conversations/${id}/recording?grant=**`, async (route) => {
    const url = new URL(route.request().url());
    const grantNumber = Number(url.searchParams.get("grant")?.match(/test-grant-(\d+)/)?.[1] ?? 0);
    const range = route.request().headers().range;
    if (!range) {
      const bytes = streamedAudio.subarray(0, 16_384);
      await route.fulfill({
        status: 206,
        contentType: "audio/wav",
        headers: { "Accept-Ranges": "bytes", "Content-Length": String(bytes.length), "Content-Range": `bytes 0-${bytes.length - 1}/${streamedAudio.length}` },
        body: bytes,
      });
      return;
    }
    const start = Number(range.match(/bytes=(\d+)-/)?.[1] ?? 0);
    if (expiredGrants.has(grantNumber) && start > 20_000) {
      expiredRanges.set(grantNumber, (expiredRanges.get(grantNumber) ?? 0) + 1);
      await route.fulfill({ status: 404, json: { error: { kind: "resource_not_found", message: "Grant expired" } } });
      return;
    }
    await fulfillAudioStream(route, streamedAudio);
  });

  await page.goto(`/recordings/${id}`);
  const audio = page.getByTestId("recording-audio");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBe(600);
  await page.getByRole("button", { name: "Jump to note at 00:02.50: Earlier marker" }).click();
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(2.4);
  await page.getByRole("button", { name: "Play recording" }).click();
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  const timeline = page.getByRole("slider", { name: "Recording timeline" });

  expiredGrants.add(1);
  await timeline.focus();
  await timeline.press("PageUp");
  await expect.poll(() => expiredRanges.get(1) ?? 0).toBeGreaterThan(0);
  await expect.poll(() => grantsIssued).toBe(2);
  await expect(audio).toHaveAttribute("src", /grant=test-grant-2/);
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(62.4);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  await page.waitForTimeout(3100);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();

  expiredGrants.add(2);
  await timeline.press("PageUp");
  await expect.poll(() => expiredRanges.get(2) ?? 0).toBeGreaterThan(0);
  await expect.poll(() => grantsIssued).toBe(3);
  await expect(audio).toHaveAttribute("src", /grant=test-grant-3/);
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(125.4);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  await expect(page.locator(".recording-player")).toHaveAttribute("data-playback-source", "stream");
});

test("failed grant refresh can be retried", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000027";
  const conversation = browserConversation(id, "2026-04-01T12:00:00Z", true);
  const streamedAudio = waveFile(600);
  let grantsIssued = 0;
  let expireFirstGrant = false;
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => {
    grantsIssued += 1;
    if (grantsIssued === 2) return route.fulfill({ status: 503, json: { error: { kind: "service_unavailable", message: "Unavailable" } } });
    return route.fulfill({ json: { grant: `test-grant-${grantsIssued}` } });
  });
  await page.route(`**/v1/conversations/${id}/recording?grant=**`, async (route) => {
    const url = new URL(route.request().url());
    const range = route.request().headers().range;
    if (expireFirstGrant && url.searchParams.get("grant") === "test-grant-1" && range && Number(range.match(/bytes=(\d+)-/)?.[1] ?? 0) > 20_000) {
      await route.fulfill({ status: 404, json: { error: { kind: "resource_not_found", message: "Grant expired" } } });
      return;
    }
    await fulfillAudioStream(route, streamedAudio, true);
  });

  await page.goto(`/recordings/${id}`);
  const audio = page.getByTestId("recording-audio");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBe(600);
  const timeline = page.getByRole("slider", { name: "Recording timeline" });
  expireFirstGrant = true;
  await timeline.focus();
  await timeline.press("End");
  await timeline.press("ArrowLeft");
  await expect(page.getByRole("alert").filter({ hasText: "Recording playback is unavailable" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry playback" })).toBeVisible();
  await page.getByRole("button", { name: "Retry playback" }).click();
  await expect.poll(() => grantsIssued).toBe(3);
  await expect(page.getByTestId("recording-audio")).toHaveAttribute("src", /grant=test-grant-3/);
  await expect.poll(() => page.getByTestId("recording-audio").evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(599.8);
});

test("a failed automatic upload can be retried", async ({ page }) => {
  const conversationKeys: string[] = [];
  const uploadKeys: string[] = [];
  let conversation: ReturnType<typeof browserConversation> | null = null;
  let uploadAttempts = 0;
  let uploaded = false;

  await installToneMicrophone(page);
  await page.route("**/v1/conversations", async (route) => {
    const request = route.request();
    if (request.method() === "GET") {
      await route.fulfill({ json: uploaded && conversation ? [browserConversation(conversation.id, conversation.occurred_at, true)] : [] });
      return;
    }
    conversationKeys.push(request.headers()["x-idempotency-key"]);
    const body = request.postDataJSON() as { id: string; occurred_at: string };
    conversation = browserConversation(body.id, body.occurred_at);
    await route.fulfill({ status: 201, json: conversation });
  });
  await page.route("**/v1/conversations/*/recording", async (route) => {
    if (route.request().method() === "GET") {
      await fulfillAudioStream(route, waveFile());
      return;
    }
    uploadKeys.push(route.request().headers()["x-idempotency-key"]);
    uploadAttempts += 1;
    if (uploadAttempts === 1) {
      await route.fulfill({
        status: 415,
        json: { error: { kind: "unsupported_media", message: "Unsupported media type" } },
      });
      return;
    }
    uploaded = true;
    await route.fulfill({ status: 201, json: {} });
  });
  await page.route("**/v1/conversations/*/recording?grant=**", (route) => fulfillAudioStream(route, waveFile()));
  await page.route("**/v1/conversations/*/recording-access", (route) => route.fulfill({ json: { grant: "retry-upload-grant" } }));
  await page.route("**/v1/conversations/*/notes", (route) => route.fulfill({ json: [] }));

  await page.goto("/");
  await page.getByRole("button", { name: "Start recording" }).click();
  await page.waitForTimeout(400);
  await page.getByRole("button", { name: "Stop recording" }).click();
  await expect(page.locator(".detail-card > .error")).toBeVisible();
  await page.getByRole("button", { name: "Retry upload" }).click();
  await page.getByRole("tab", { name: "Status" }).click();
  await expect(page.locator(".status-row").filter({ hasText: "Recording" })).toContainText("UPLOADED");
  await expect(page.getByRole("button", { name: "Play recording" })).toBeVisible();
  await expect(page.getByTestId("recording-audio")).toBeAttached();

  expect(conversationKeys).toHaveLength(2);
  expect(conversationKeys[0]).toBe(conversationKeys[1]);
  expect(uploadKeys).toHaveLength(2);
  expect(uploadKeys[0]).toBe(uploadKeys[1]);
});

test("details edit metadata in a dialog and add notes at the playhead", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000002";
  const noteId = "00000000-0000-0000-0000-000000000003";
  let conversation: Conversation = browserConversation(id, "2026-01-01T00:00:00Z", true);
  let updateKey: string | undefined;
  const notes: Array<{
    id: string;
    conversation_id: string;
    offset_seconds: number;
    text: string;
    subject: string;
    author_kind: string;
    delegated_session_id: null;
    created_at: string;
  }> = [];

  await installLocalAuth(page);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}`, async (route) => {
    if (route.request().method() !== "PATCH") {
      await route.continue();
      return;
    }
    updateKey = route.request().headers()["x-idempotency-key"];
    const changes = route.request().postDataJSON() as { name: string; description: string | null; people: string[]; tags: string[] };
    conversation = { ...conversation, ...changes, revision: conversation.revision + 1 };
    await route.fulfill({ status: 200, json: conversation });
  });
  let rangeRequests = 0;
  let grantsIssued = 0;
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => {
    grantsIssued += 1;
    return route.fulfill({ json: { grant: `test-grant-${grantsIssued}` } });
  });
  await page.route(new RegExp(`/v1/conversations/${id}/recording(?:\\?.*)?$`), async (route) => {
    const url = new URL(route.request().url());
    expect(url.searchParams.get("mode")).toBeNull();
    expect(route.request().headers().range).toBeDefined();
    rangeRequests += 1;
    await fulfillAudioStream(route, waveFile());
  });
  await page.route(`**/v1/conversations/${id}/notes/${noteId}`, async (route) => {
    const payload = route.request().postDataJSON() as { text: string };
    const existing = notes.find((note) => note.id === noteId);
    if (!existing) throw new Error("The note was not created before its edit request.");
    const updated = { ...existing, text: payload.text };
    notes[notes.indexOf(existing)] = updated;
    await route.fulfill({ json: updated });
  });
  await page.route(`**/v1/conversations/${id}/notes`, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: notes });
      return;
    }
    const payload = route.request().postDataJSON() as { offset_seconds: number; text: string };
    const note = {
      id: noteId,
      conversation_id: id,
      offset_seconds: payload.offset_seconds,
      text: payload.text,
      subject: "local-development",
      author_kind: "human",
      delegated_session_id: null,
      created_at: "2026-01-01T00:00:01Z",
    };
    notes.push(note);
    await route.fulfill({ status: 201, json: note });
  });

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  await expect.poll(() => page.getByTestId("recording-audio").evaluate((element) => (element as HTMLAudioElement).duration)).toBe(10);
  const actionsTrigger = page.getByRole("button", { name: "Conversation actions" });
  await actionsTrigger.click();
  const downloads = await openDownloadSubmenu(page);
  await expect(downloads.getByRole("menuitem", { name: "Download recording" })).toBeEnabled();
  await actionsTrigger.click();
  expect(rangeRequests).toBeGreaterThan(0);
  const bufferingStatus = page.getByText("Buffering…");
  await expect(bufferingStatus).toBeHidden();
  await expect(page.getByRole("button", { name: "Play recording" })).toBeVisible();
  await page.getByRole("tab", { name: "Notes" }).click();
  await expect(page.getByRole("heading", { name: "Timeline notes" })).toBeVisible();
  await expect(page.getByText("Remember this")).toHaveCount(0);
  await expect(page.getByText("No notes yet")).toHaveCount(0);
  await page.getByRole("tab", { name: "Status" }).click();
  const statusRows = page.locator(".status-rows");
  await expect(statusRows.getByText("Recording")).toBeVisible();
  await expect(statusRows.getByText("UPLOADED")).toBeVisible();
  await expect(statusRows.getByText("Transcription")).toBeVisible();
  await expect(statusRows.getByText("Summary")).toBeVisible();
  await expect(page.getByLabel("Title")).toBeHidden();
  await page.getByRole("button", { name: "Conversation actions" }).click();
  await page.getByRole("menuitem", { name: "Edit details" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Title").fill("Updated title");
  await dialog.getByLabel("Description").fill("A useful description");
  await dialog.getByLabel("People").fill("Ada Lovelace, Grace Hopper");
  await dialog.getByLabel("Tags").fill("follow-up, design");
  await dialog.getByRole("button", { name: "Save changes" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("heading", { name: "Updated title" })).toBeVisible();
  await expect(page.locator(".detail-description")).toHaveText("A useful description");
  await expect(page.getByText("Ada Lovelace")).toBeVisible();
  await expect(page.getByText("follow-up")).toBeVisible();
  expect(updateKey).toMatch(/^[0-9a-f-]{36}$/);
  await expect(page.locator(".recording-player").getByRole("button", { name: "Export recording" })).toHaveCount(0);
  await expect(page.locator(".recording-player").getByRole("link", { name: "Download recording" })).toHaveCount(0);

  const timeline = page.getByRole("slider", { name: "Recording timeline" });
  const audio = page.getByTestId("recording-audio");
  await actionsTrigger.click();
  const reopenedDownloads = await openDownloadSubmenu(page);
  await expect(reopenedDownloads.getByRole("menuitem", { name: "Download recording" })).toBeEnabled();
  await actionsTrigger.click();
  await expect(audio).toHaveAttribute("src", /\/v1\/conversations\/.+\/recording\?grant=test-grant-1/);
  await expect(page.locator(".recording-player")).toHaveAttribute("data-playback-source", "stream");
  await expect(page.locator(".play-toggle")).toBeEnabled();
  await expect(page.getByText("Buffering…")).toHaveCount(0);
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBe(10);
  await expect(timeline).toBeEnabled();
  await expect(timeline).toHaveAttribute("max", "10");
  await timeline.focus();
  await timeline.press("ArrowRight");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThan(0);
  await expect(page.getByText("Buffering…")).toHaveCount(0);
  await page.getByRole("button", { name: "Play recording" }).click();
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  const seekAudio = (seconds: number) => audio.evaluate((element, target) => new Promise<void>((resolve) => {
    const player = element as HTMLAudioElement;
    if (!player.seeking && Math.abs(player.currentTime - target) < 0.001) {
      resolve();
      return;
    }
    player.addEventListener("seeked", () => resolve(), { once: true });
    player.currentTime = target;
  }), seconds);
  const timeBeforeLocalSeek = await audio.evaluate((element) => (element as HTMLAudioElement).currentTime);
  await seekAudio(4.237);
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).not.toBe(timeBeforeLocalSeek);
  await seekAudio(6.5);
  await seekAudio(4.237);
  const addNoteButton = page.getByRole("button", { name: "Add note" });
  await expect(addNoteButton).toHaveAttribute("title", "Add a note while listening to pin a thought to this moment.");
  await addNoteButton.click();
  const rangeBox = await timeline.boundingBox();
  if (!rangeBox) throw new Error("The recording timeline is not visible.");
  const sliderStart = rangeBox.x + 8;
  const sliderEnd = rangeBox.x + rangeBox.width - 8;
  const sliderY = rangeBox.y + rangeBox.height / 2;
  const rangeMaximum = Number(await timeline.getAttribute("max"));
  const currentSliderValue = Number(await timeline.inputValue());
  await page.mouse.move(sliderStart + (sliderEnd - sliderStart) * currentSliderValue / rangeMaximum, sliderY);
  await page.mouse.down();
  await page.mouse.move(sliderStart + (sliderEnd - sliderStart) * 0.8251, sliderY, { steps: 10 });
  const pointerMilliseconds = Math.round(Number(await timeline.inputValue()) * 1000);
  const pointerTime = `${String(Math.floor(pointerMilliseconds / 60_000)).padStart(2, "0")}:${String(Math.floor(pointerMilliseconds / 1000) % 60).padStart(2, "0")}.${String(Math.floor(pointerMilliseconds % 1000 / 10)).padStart(2, "0")}`;
  await expect(page.locator(".note-composer label")).toHaveText(`Note at ${pointerTime}`);
  await page.mouse.up();
  await timeline.focus();
  await timeline.press("ArrowRight");
  const clickedOffset = Math.round(Number(await timeline.inputValue()) * 1000) / 1000;
  const totalMilliseconds = Math.round(clickedOffset * 1000);
  const noteTime = `${String(Math.floor(totalMilliseconds / 60_000)).padStart(2, "0")}:${String(Math.floor(totalMilliseconds / 1000) % 60).padStart(2, "0")}.${String(totalMilliseconds % 1000).padStart(3, "0")}`;
  await expect(page.locator(".note-composer label")).toHaveText(`Note at ${noteTime.slice(0, -1)}`);
  await seekAudio(1);
  await expect(page.locator(".note-composer label")).toHaveText(`Note at ${noteTime.slice(0, -1)}`);
  const longNote = "Follow up on this point with additional context. ".repeat(12).trim();
  const noteComposer = page.locator(".note-composer textarea");
  await expect(noteComposer).toBeVisible();
  await noteComposer.fill(longNote);
  await page.getByRole("button", { name: "Save note" }).click();
  await page.getByRole("tab", { name: "Notes" }).click();
  await expect(page.locator(".note-row")).toContainText(longNote);
  await expect(page.locator(".note-seek-button")).toHaveCount(1);
  await expect(page.locator(".note-text-button")).toHaveCount(1);
  await expect(page.locator(".note-row .note-edit-button")).toBeVisible();
  const editNote = page.locator(".note-row .note-edit-button");
  await expect(page.locator(".note-row.active")).toHaveAttribute("aria-current", "true");
  expect(notes[0]?.offset_seconds).toBe(clickedOffset);
  const noteTimestampButton = page.locator(".note-seek-button").filter({ hasText: noteTime.slice(0, -1) });
  await expect(noteTimestampButton).toBeVisible();
  await noteTimestampButton.click();
  expect(notes[0]?.offset_seconds).toBe(clickedOffset);
  await editNote.click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(page.locator(".note-row")).toHaveCount(1);
  await seekAudio(1);
  expect(notes[0]?.offset_seconds).toBe(clickedOffset);
  const notePreview = page.locator(".note-preview");
  await expect(notePreview).toHaveCSS("white-space", "nowrap");
  await expect(notePreview).toHaveCSS("text-overflow", "ellipsis");
  await expect.poll(() => notePreview.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);

  await editNote.click();
  const noteDialog = page.getByRole("dialog");
  await expect(noteDialog).toBeVisible();
  await expect(noteDialog.locator(".note-detail-timestamp")).toHaveText(`${noteTime.slice(0, -1)} in recording`);
  const noteText = noteDialog.getByLabel("Note text");
  await expect(noteText).toHaveValue(longNote);
  await noteText.fill("Revised follow-up");
  await noteDialog.getByRole("button", { name: "Save note" }).click();
  await expect(noteDialog).toBeHidden();
  await expect(page.locator(".note-preview")).toHaveText("Revised follow-up");
  expect(notes[0]?.text).toBe("Revised follow-up");
  expect(rangeRequests).toBeGreaterThan(0);
  await expect(audio).toHaveAttribute("src", /\/v1\/conversations\/.+\/recording\?grant=test-grant-1/);
  await expect(page.locator(".recording-player")).toHaveAttribute("data-playback-source", "stream");
});

test("adding notes preserves playback state and save failures keep their draft until cancel", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000041";
  const conversation = browserConversation(id, "2026-04-01T12:00:00Z", true);
  const notes: Array<{
    id: string;
    conversation_id: string;
    offset_seconds: number;
    text: string;
    subject: string;
    author_kind: "human";
    delegated_session_id: null;
    created_at: string;
  }> = [];
  let createRequests = 0;
  const pendingSaves = new Map<number, () => void>();
  await installLocalAuth(page);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}`, (route) => route.fulfill({ json: conversation }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/notes`, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: notes });
      return;
    }
    createRequests += 1;
    if (createRequests === 1 || createRequests === 3) {
      await new Promise<void>((resolve) => { pendingSaves.set(createRequests, resolve); });
    }
    if (createRequests === 3) {
      await route.fulfill({ status: 503, json: { error: { kind: "service_unavailable", message: "Unavailable" } } });
      return;
    }
    const payload = route.request().postDataJSON() as { offset_seconds: number; text: string };
    const note = {
      id: `note-${createRequests}`,
      conversation_id: id,
      offset_seconds: payload.offset_seconds,
      text: payload.text,
      subject: "local-development",
      author_kind: "human" as const,
      delegated_session_id: null,
      created_at: "2026-04-01T12:00:01Z",
    };
    notes.push(note);
    await route.fulfill({ status: 201, json: note });
  });
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => route.fulfill({ json: { grant: "note-playback" } }));
  await page.route(`**/v1/conversations/${id}/recording?grant=**`, (route) => fulfillAudioStream(route, waveFile()));

  await page.goto(`/recordings/${id}`);
  const audio = page.getByTestId("recording-audio");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBe(10);
  await audio.evaluate((element) => {
    const player = element as HTMLAudioElement;
    player.dataset.playEvents = "0";
    player.addEventListener("play", () => {
      player.dataset.playEvents = String(Number(player.dataset.playEvents) + 1);
    });
  });

  await page.getByRole("button", { name: "Play recording" }).click();
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  const addNoteButton = page.getByRole("button", { name: "Add note" });
  await addNoteButton.click();
  const firstDraft = page.locator(".note-composer textarea");
  await firstDraft.fill("Saved while playing");
  await page.getByRole("button", { name: "Save note" }).click();
  await expect.poll(() => pendingSaves.has(1)).toBe(true);
  await expect(addNoteButton).toBeDisabled();
  await expect(firstDraft).toBeDisabled();
  await expect(page.getByRole("button", { name: "Cancel" })).toBeDisabled();
  await page.locator(".note-composer").dispatchEvent("submit");
  await expect.poll(() => createRequests).toBe(1);
  pendingSaves.get(1)?.();
  await expect(page.locator(".note-composer")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  await expect(audio).toHaveAttribute("data-play-events", "2");

  await page.getByRole("button", { name: "Pause recording" }).click();
  await expect(page.getByRole("button", { name: "Play recording" })).toBeVisible();
  await page.getByRole("button", { name: "Add note" }).click();
  await page.locator(".note-composer textarea").fill("Saved while paused");
  await page.getByRole("button", { name: "Save note" }).click();
  await expect(page.locator(".note-composer")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Play recording" })).toBeVisible();
  await expect(audio).toHaveAttribute("data-play-events", "2");

  await page.getByRole("button", { name: "Play recording" }).click();
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  await expect(audio).toHaveAttribute("data-play-events", "3");
  await page.getByRole("button", { name: "Add note" }).click();
  const draft = page.locator(".note-composer textarea");
  await draft.fill("Keep this after a failed save");
  await page.getByRole("button", { name: "Save note" }).click();
  await expect.poll(() => pendingSaves.has(3)).toBe(true);
  await expect(addNoteButton).toBeDisabled();
  await expect(draft).toBeDisabled();
  await expect(page.getByRole("button", { name: "Cancel" })).toBeDisabled();
  await page.locator(".note-composer").dispatchEvent("submit");
  await expect.poll(() => createRequests).toBe(3);
  pendingSaves.get(3)?.();
  await expect(page.getByRole("alert").filter({ hasText: "The note could not be saved." })).toBeVisible();
  await expect(draft).toHaveValue("Keep this after a failed save");
  await expect(page.getByRole("button", { name: "Play recording" })).toBeVisible();
  await expect(audio).toHaveAttribute("data-play-events", "3");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator(".note-composer")).toHaveCount(0);
  await expect(page.getByRole("alert").filter({ hasText: "The note could not be saved." })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  await expect(audio).toHaveAttribute("data-play-events", "4");
});

test("canceling a pointer scrub restores the timeline and note timestamp", async ({ page }) => {
  const { audio, timeline } = await openNoteComposerForScrubTest(page, "00000000-0000-0000-0000-000000000051");
  await expect(timeline).toHaveValue("0");
  const noteTimestamp = page.locator(".note-composer label");
  await expect(noteTimestamp).toHaveText("Note at 00:00.00");
  const rangeBox = await timeline.boundingBox();
  if (!rangeBox) throw new Error("The recording timeline is not visible.");
  const sliderStart = rangeBox.x + 8;
  const sliderEnd = rangeBox.x + rangeBox.width - 8;
  const sliderY = rangeBox.y + rangeBox.height / 2;

  for (const cancelEvent of ["pointercancel", "lostpointercapture"] as const) {
    await timeline.evaluate((element) => {
      element.addEventListener("pointerdown", (event) => {
        element.dataset.scrubPointerId = String((event as PointerEvent).pointerId);
      }, { once: true });
    });
    const currentValue = Number(await timeline.inputValue());
    await page.mouse.move(sliderStart + (sliderEnd - sliderStart) * currentValue / 10, sliderY);
    await page.mouse.down();
    await page.mouse.move(sliderStart + (sliderEnd - sliderStart) * 0.8, sliderY, { steps: 8 });
    await expect(noteTimestamp).not.toHaveText("Note at 00:00.00");
    const pointerId = Number(await timeline.getAttribute("data-scrub-pointer-id"));
    await timeline.dispatchEvent(cancelEvent, { pointerId, pointerType: "mouse", bubbles: true });
    await page.mouse.up();
    await expect(noteTimestamp).toHaveText("Note at 00:00.00");
    await expect(timeline).toHaveValue("0");
    await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBe(0);
  }
});

test("scrubbing is locked to the submitted timestamp while a note save is pending", async ({ page }) => {
  const { audio, timeline, saveRequest, releaseSave } = await openNoteComposerForScrubTest(page, "00000000-0000-0000-0000-000000000052");
  await timeline.focus();
  await timeline.press("End");
  const noteTimestamp = page.locator(".note-composer label");
  await expect(noteTimestamp).toHaveText("Note at 00:10.00");
  await page.locator(".note-composer textarea").fill("Pinned while saving");
  await page.getByRole("button", { name: "Save note" }).click();
  const submittedNote = await saveRequest;
  expect(submittedNote).toEqual({ offset_seconds: 10, text: "Pinned while saving" });
  await expect(timeline).toBeDisabled();
  await expect(noteTimestamp).toHaveText("Note at 00:10.00");
  const rangeBox = await timeline.boundingBox();
  if (!rangeBox) throw new Error("The recording timeline is not visible.");
  await page.mouse.click(rangeBox.x + rangeBox.width * 0.2, rangeBox.y + rangeBox.height / 2);
  await expect(timeline).toHaveValue("10");
  await expect(noteTimestamp).toHaveText("Note at 00:10.00");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBe(10);
  releaseSave();
  await expect(page.locator(".note-composer")).toHaveCount(0);
});

test("timeline note marker tooltips appear on hover and keyboard focus with text capped at 128 characters", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000042";
  const conversation = browserConversation(id, "2026-04-01T12:00:00Z", true);
  const shortText = "Remember this point";
  const longText = "A".repeat(160);
  const notes = [
    {
      id: "note-short",
      conversation_id: id,
      offset_seconds: 2,
      text: shortText,
      subject: "local-development",
      author_kind: "human",
      delegated_session_id: null,
      created_at: "2026-04-01T12:00:01Z",
    },
    {
      id: "note-long",
      conversation_id: id,
      offset_seconds: 6,
      text: longText,
      subject: "local-development",
      author_kind: "human",
      delegated_session_id: null,
      created_at: "2026-04-01T12:00:02Z",
    },
  ];
  await installLocalAuth(page);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}`, (route) => route.fulfill({ json: conversation }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: notes }));
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => route.fulfill({ json: { grant: "marker-title" } }));
  await page.route(`**/v1/conversations/${id}/recording?grant=**`, (route) => fulfillAudioStream(route, waveFile()));

  await page.goto(`/recordings/${id}`);
  const audio = page.getByTestId("recording-audio");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBe(10);
  const markers = page.locator(".timeline-marker");
  await expect(markers).toHaveCount(2);
  const shortMarker = markers.nth(0);
  const longMarker = markers.nth(1);
  const tooltip = page.getByRole("tooltip");
  await shortMarker.hover();
  await expect(tooltip).toHaveText(shortText);
  await expect(shortMarker).not.toHaveAttribute("title");
  await expect(shortMarker).toHaveAttribute("aria-describedby", /timeline-note-tooltip/);
  await expect(shortMarker).toHaveAttribute("aria-label", `Jump to note at 00:02.00: ${shortText}`);
  await page.waitForTimeout(750);
  await expect(tooltip).toHaveCount(1);
  await expect(shortMarker).not.toHaveAttribute("title");

  await page.mouse.move(0, 0);
  await expect(tooltip).toHaveCount(0);
  await page.getByRole("slider", { name: "Recording timeline" }).focus();
  await page.keyboard.press("Tab");
  await expect(shortMarker).toBeFocused();
  await expect(tooltip).toHaveText(shortText);
  await page.keyboard.press("Tab");
  await expect(longMarker).toBeFocused();
  await expect(tooltip).toHaveText(`${"A".repeat(127)}…`);
  await expect(longMarker).not.toHaveAttribute("title");
  await expect(longMarker).toHaveAttribute("aria-describedby", /timeline-note-tooltip/);
  await expect(longMarker).toHaveAttribute("aria-label", `Jump to note at 00:06.00: ${longText}`);
  const tooltipText = await tooltip.textContent();
  expect(Array.from(tooltipText ?? "")).toHaveLength(128);
  await shortMarker.click();
  await expect(page.getByRole("tab", { name: "Notes" })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator('.note-row[data-note-id="note-short"]')).toHaveAttribute("aria-current", "true");
});

test("imports an audio file into a recording with its original filename", async ({ page }) => {
  let conversation: Conversation | null = null;
  let uploaded = false;
  let uploadBody = "";

  await page.route("**/v1/conversations", async (route) => {
    if (route.request().method() === "GET") {
      const recordings = conversation ? [{ ...conversation, recording_available: uploaded, sync_state: uploaded ? "processing" : "local_only" }] : [];
      await route.fulfill({ json: recordings });
      return;
    }
    const payload = route.request().postDataJSON() as { id: string; name: string; occurred_at: string };
    conversation = { ...browserConversation(payload.id, payload.occurred_at), name: payload.name };
    await route.fulfill({ status: 201, json: conversation });
  });
  await page.route("**/v1/conversations/*/recording", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ contentType: "audio/wav", body: waveFile() });
      return;
    }
    uploadBody = route.request().postDataBuffer()?.toString("latin1") ?? "";
    uploaded = true;
    await route.fulfill({ status: 201, json: {} });
  });
  await page.route("**/v1/conversations/*/recording?grant=**", (route) => fulfillAudioStream(route, waveFile()));
  await page.route("**/v1/conversations/*/recording-access", (route) => route.fulfill({ json: { grant: "imported-recording-grant" } }));
  await page.route("**/v1/conversations/*/notes", (route) => route.fulfill({ json: [] }));

  await page.goto("/");
  await page.getByLabel("Import audio file").setInputFiles({
    name: "team-sync.wav",
    mimeType: "audio/wav",
    buffer: waveFile(),
  });

  await expect(page).toHaveURL(/\/recordings\/[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { name: "team-sync" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Play recording" })).toBeVisible();
  expect(uploadBody).toContain('filename="team-sync.wav"');
  expect(uploadBody).toContain("Content-Type: audio/wav");
});

test("one transient API failure does not show the global maintenance screen", async ({ page }) => {
  let requests = 0;
  await page.route("**/v1/conversations", async (route) => {
    requests += 1;
    if (requests === 1) {
      await route.fulfill({ status: 503, json: { error: { kind: "service_unavailable", message: "Unavailable" } } });
      return;
    }
    await route.fulfill({ json: [] });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Maintenance" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Recordings", exact: true })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "temporarily unavailable" })).toBeVisible();
  await expect.poll(() => requests, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);
  await expect(page.getByRole("alert").filter({ hasText: "temporarily unavailable" })).toHaveCount(0);
});

test("reloading a conversation with a responsive API does not show Connecting to Muninn", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000023";
  const conversation = { ...browserConversation(id, "2026-04-01T12:00:00Z", true), name: "Reloadable conversation" };
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => route.fulfill({ json: { grant: "test-grant" } }));
  await page.route(`**/v1/conversations/${id}/recording`, (route) => route.fulfill({ contentType: "audio/wav", body: waveFile() }));

  await page.goto(`/recordings/${id}`);
  await expect(page.getByRole("heading", { name: "Reloadable conversation" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Connecting to Muninn…" })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Reloadable conversation" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Connecting to Muninn…" })).toHaveCount(0);
});

test("maintenance page automatically clears when the API recovers", async ({ page }) => {
  let requests = 0;
  await page.route("**/v1/conversations", async (route) => {
    requests += 1;
    if (requests < 5) {
      await route.fulfill({ status: 503, json: { error: { kind: "service_unavailable", message: "Unavailable" } } });
      return;
    }
    await route.fulfill({ json: [] });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Maintenance" })).toBeVisible({ timeout: 40_000 });
  await expect.poll(() => requests, { timeout: 30_000 }).toBe(5);
  await expect(page.getByRole("heading", { name: "Recordings", exact: true })).toBeVisible();
});

test("maintenance preserves an in-progress note draft until the API recovers", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000022";
  const conversation = { ...browserConversation(id, "2026-04-01T12:00:00Z", true), name: "Important conversation" };
  let apiAvailable = true;
  let failedRequests = 0;

  await page.route("**/v1/conversations", async (route) => {
    if (!apiAvailable) {
      failedRequests += 1;
      await route.fulfill({ status: 503, json: { error: { kind: "service_unavailable", message: "Unavailable" } } });
      return;
    }
    await route.fulfill({ json: [conversation] });
  });
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => route.fulfill({ json: { grant: "test-grant" } }));
  await page.route(`**/v1/conversations/${id}/recording`, (route) => route.fulfill({ contentType: "audio/wav", body: waveFile() }));
  await page.route(`**/v1/conversations/${id}/recording?grant=**`, (route) => fulfillAudioStream(route, waveFile()));

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  await page.getByRole("tab", { name: "Notes" }).click();
  await expect(page.getByRole("heading", { name: "Timeline notes" })).toBeVisible();
  await page.getByRole("button", { name: "Add note" }).click();
  const noteDraft = page.locator("#current-note");
  await noteDraft.fill("Draft saved while the API is offline");

  apiAvailable = false;
  await expect(page.getByRole("heading", { name: "Maintenance" })).toHaveCount(0);
  await expect(noteDraft).toHaveValue("Draft saved while the API is offline");
  await expect.poll(() => failedRequests, { timeout: 15_000 }).toBeGreaterThanOrEqual(3);
  await expect(page.getByRole("heading", { name: "Maintenance" })).toBeVisible({ timeout: 20_000 });
  await expect(noteDraft).toHaveValue("Draft saved while the API is offline");

  apiAvailable = true;
  await expect(page.getByRole("heading", { name: "Maintenance" })).toBeHidden({ timeout: 10000 });
  await expect(noteDraft).toHaveValue("Draft saved while the API is offline");
});

test("recordings search and filters update live and sort in either direction", async ({ page }) => {
  const conversations: Conversation[] = [
    {
      ...browserConversation("00000000-0000-0000-0000-000000000011", "2026-04-03T12:00:00Z", true),
      name: "Zulu plan",
      description: "Quarterly strategy",
      people: ["Ada Lovelace"],
      tags: ["planning", "work"],
    },
    {
      ...browserConversation("00000000-0000-0000-0000-000000000012", "2026-04-01T12:00:00Z", true),
      name: "Alpha therapy",
      description: "Initial counseling session",
      people: ["Tucker Beck"],
      tags: ["health"],
    },
    {
      ...browserConversation("00000000-0000-0000-0000-000000000013", "2026-04-02T12:00:00Z", true),
      name: "Beta expedition",
      description: "Cold weather field notes",
      people: ["Ada Lovelace"],
      tags: ["outdoors", "travel"],
    },
  ];
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: conversations }));
  await page.goto("/");
  const names = () => page.locator(".recording-row-main strong").allTextContents();
  const search = page.getByLabel("Search recordings");
  const filterPanel = page.locator("details.recording-filter-panel");
  await expect(search).toBeVisible();
  await expect(filterPanel.locator("summary")).toHaveText("Filters");
  await expect(page.getByLabel("Filter by tag")).toBeHidden();
  await filterPanel.locator("summary").click();
  await expect(page.getByLabel("Filter by tag")).toBeVisible();

  expect(await names()).toEqual(["Zulu plan", "Beta expedition", "Alpha therapy"]);
  await search.fill("counseling");
  expect(await names()).toEqual(["Alpha therapy"]);
  await search.fill("");
  await page.getByLabel("Filter by tag").selectOption("outdoors");
  expect(await names()).toEqual(["Beta expedition"]);
  await page.getByLabel("Filter by tag").selectOption("");
  await page.getByLabel("Filter by people").selectOption("Ada Lovelace");
  expect(await names()).toEqual(["Zulu plan", "Beta expedition"]);
  await page.getByLabel("Filter by people").selectOption("");

  await page.getByLabel("Sort recordings by").selectOption("name");
  expect(await names()).toEqual(["Alpha therapy", "Beta expedition", "Zulu plan"]);
  await page.getByRole("button", { name: /Reverse sort order/ }).click();
  expect(await names()).toEqual(["Zulu plan", "Beta expedition", "Alpha therapy"]);
  await page.getByLabel("Sort recordings by").selectOption("date");
  expect(await names()).toEqual(["Zulu plan", "Beta expedition", "Alpha therapy"]);
  await page.getByRole("button", { name: /Reverse sort order/ }).click();
  expect(await names()).toEqual(["Alpha therapy", "Beta expedition", "Zulu plan"]);
  await filterPanel.locator("summary").click();
  await expect(page.getByLabel("Filter by tag")).toBeHidden();
});

test("conversation review displays the diarized transcript", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000004";
  const conversation = browserConversation(id, "2026-01-01T00:00:00Z", true);
  conversation.sync_state = "ready";
  conversation.transcript_available = true;
  conversation.summary_available = true;
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [
    { id: "job-1", kind: "transcription", state: "failed", attempts: 1, created_at: "2026-01-01T00:00:01Z" },
  ] }));
  await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({
    json: {
      language: "en",
      segments: Array.from({ length: 12 }, (_, index) => ({
        evidence_id: `segment-${index + 1}`,
        speaker: index % 2 === 0 ? "Speaker 1" : "Speaker 2",
        start_seconds: index * 0.7,
        end_seconds: index * 0.7 + 0.55,
        text: index === 0 ? "The transcription is ready." : `Transcript segment ${index + 1}.`,
      })),
      provider: "assemblyai",
      model: "universal-3-5-pro",
    },
  }));
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => route.fulfill({ json: { grant: "test-grant" } }));
  await page.route(`**/v1/conversations/${id}/recording?grant=**`, (route) => fulfillAudioStream(route, waveFile()));
  await page.route(`**/v1/conversations/${id}/summary`, (route) => route.fulfill({
    json: {
      overview: {
        text: "They agreed to schedule a follow-up.",
        evidence_span_ids: ["segment-1"],
        confidence: "certain",
        owner: null,
        due_date: null,
      },
      key_topics: [],
      decisions: [],
      action_items: [],
      provider: "assemblyai",
      model: "qwen3.5-4b-32k-fast",
    },
  }));

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  await page.getByRole("tab", { name: "Transcript" }).click();

  await expect(page.getByRole("heading", { name: "Transcript" })).toBeVisible();
  await expect(page.locator(".transcript-segment").first().locator(".speaker-name")).toHaveText("Speaker 1");
  await expect(page.getByText("The transcription is ready.")).toBeVisible();
  await page.getByRole("tab", { name: "Status" }).click();
  const transcriptionStatus = page.locator(".status-row").filter({ hasText: "Transcription" });
  await expect(transcriptionStatus.locator(".status-value")).toHaveText("FAILED");
  await expect(transcriptionStatus.locator(".status-row-issue")).toContainText("The current transcript is available, but the latest attempt failed.");
  await page.getByRole("tab", { name: "Transcript" }).click();
  await page.getByRole("button", { name: "Play recording" }).click();
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  const audio = page.getByTestId("recording-audio");
  await audio.evaluate((element) => {
    const player = element as HTMLAudioElement;
    player.currentTime = 8.1;
    player.dispatchEvent(new Event("timeupdate"));
  });
  const activeSegment = page.locator(".transcript-segment.active");
  await expect(activeSegment).toHaveAttribute("aria-current", "true");
  await expect(activeSegment).toContainText("Transcript segment 12.");
  await expect.poll(() => page.locator(".review-panel").evaluate((panel) => panel.scrollTop)).toBeGreaterThan(0);
  await page.getByRole("tab", { name: "Summary" }).click();
  await expect(page.getByRole("heading", { name: "Summary" })).toBeVisible();
  await expect(page.getByText("They agreed to schedule a follow-up.")).toBeVisible();
});

test("conversation review shows RUNNING with indeterminate transcription progress", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000005";
  const conversation = browserConversation(id, "2026-01-01T00:00:00Z", true);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [
    { id: "job-running", kind: "transcription", state: "running", attempts: 1, created_at: "2026-01-01T00:00:01Z" },
  ] }));

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  await page.getByRole("tab", { name: "Status" }).click();

  const transcriptionRow = page.locator(".status-row").filter({ hasText: "Transcription" });
  await expect(transcriptionRow.locator(".status-value")).toHaveText("RUNNING");
  await expect(transcriptionRow.getByRole("progressbar", { name: "Transcription progress" })).toBeVisible();
});

test("conversation review stages a transcription job when none exists", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000006";
  const conversation = browserConversation(id, "2026-01-01T00:00:00Z", true);
  let job: { id: string; kind: string; state: string; attempts: number; created_at: string } | null = null;
  let stageKey: string | undefined;
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: job ? [job] : [] }));
  await page.route(`**/v1/conversations/${id}/jobs/transcription`, async (route) => {
    stageKey = route.request().headers()["x-idempotency-key"];
    job = { id: "job-staged", kind: "transcription", state: "queued", attempts: 0, created_at: "2026-01-01T00:00:01Z" };
    await route.fulfill({ status: 202, json: job });
  });

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  await page.getByRole("tab", { name: "Status" }).click();

  const transcriptionRow = page.locator(".status-row").filter({ hasText: "Transcription" });
  await expect(transcriptionRow.locator(".status-value")).toHaveText("STAGED");
  await expect(transcriptionRow.getByRole("progressbar", { name: "Transcription progress" })).toBeVisible();
  expect(stageKey).toMatch(/^[0-9a-f-]{36}$/);
});

test("transcription and summary pages show one accessible progress status for queued and running jobs", async ({ browser }) => {
  const id = "00000000-0000-0000-0000-000000000064";
  const transcript = {
    language: "en" as const,
    segments: [{ evidence_id: "segment-1", speaker: "Speaker 1", start_seconds: 0, end_seconds: 1, text: "The existing transcript remains visible." }],
    provider: "test",
    model: "test",
  };
  const summary = {
    overview: { text: "The existing summary remains visible.", evidence_span_ids: ["segment-1"], confidence: "certain" as const, owner: null, due_date: null },
    key_topics: [],
    decisions: [],
    action_items: [],
    provider: "test",
    model: "test",
  };
  for (const kind of ["transcription", "summarization"] as const) {
    for (const state of ["queued", "running"] as const) {
      const context = await browser.newContext();
      const page = await context.newPage();
      const conversation = {
        ...browserConversation(id, "2026-04-04T12:00:00Z", true),
        transcript_available: true,
        summary_available: true,
      };
      const job = {
        id: `${kind}-job`,
        kind,
        state,
        attempts: state === "running" ? 1 : 0,
        created_at: "2026-04-04T12:00:01Z",
        last_error_code: null,
        last_error_details: null,
      };
      const artifactTab = kind === "transcription" ? "Transcript" : "Summary";
      const progressName = kind === "transcription" ? "Transcription progress" : "Summary progress";
      const artifactText = kind === "transcription" ? transcript.segments[0]!.text : summary.overview.text;
      const visibleStatus = state === "queued" ? "STAGED" : "RUNNING";
      await installLocalAuth(page, ["processing:admin"]);
      await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
      await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
      await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [job] }));
      await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({ json: transcript }));
      await page.route(`**/v1/conversations/${id}/summary`, (route) => route.fulfill({ json: summary }));
      await page.route(`**/v1/conversations/${id}/recording-access`, (route) => route.fulfill({ json: { grant: "progress-test-grant" } }));
      await page.route(`**/v1/conversations/${id}/recording?grant=**`, (route) => fulfillAudioStream(route, waveFile()));

      await page.goto("/");
      await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
      await page.getByRole("tab", { name: artifactTab }).click();
      const progress = page.locator(".review-panel .transcript-progress");
      await expect(progress).toHaveCount(1);
      await expect(progress.locator("strong")).toHaveText(visibleStatus);
      await expect(progress.getByRole("progressbar", { name: progressName })).toHaveAttribute("aria-valuetext", visibleStatus);
      await expect(page.locator(".review-panel [role='status']")).toHaveCount(1);
      await expect(page.locator(".review-panel").getByText(visibleStatus, { exact: true })).toHaveCount(1);
      await expect(page.getByText(artifactText)).toBeVisible();

      await page.getByRole("tab", { name: "Status" }).click();
      const statusRow = page.locator(".status-row").filter({ hasText: kind === "transcription" ? "Transcription" : "Summary" });
      await expect(statusRow.locator(".status-value")).toHaveText(visibleStatus);
      await expect(statusRow.getByRole("progressbar", { name: progressName })).toHaveAttribute("aria-valuetext", visibleStatus);
      await expect(statusRow.getByRole("progressbar", { name: progressName })).toHaveCount(1);
      await context.close();
    }
  }
});

test("latest failed or cancelled summarization is reported while the previous summary remains available", async ({ browser }) => {
  const id = "00000000-0000-0000-0000-000000000076";
  const summary = {
    overview: { text: "The previous summary remains available.", evidence_span_ids: ["segment-1"], confidence: "certain" as const, owner: null, due_date: null },
    key_topics: [],
    decisions: [],
    action_items: [],
    provider: "test",
    model: "previous",
  };
  for (const state of ["failed", "cancelled"] as const) {
    const context = await browser.newContext();
    const page = await context.newPage();
    const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z", true), transcript_available: true, summary_available: true };
    const details = state === "failed" ? "The summary provider failed." : "The summary attempt was cancelled.";
    const job = {
      id: `summary-${state}-job`,
      kind: "summarization",
      state,
      attempts: 1,
      created_at: "2026-04-04T12:00:01Z",
      last_error_code: state === "failed" ? "provider_error" : "cancelled",
      last_error_details: details,
    };
    await installLocalAuth(page, ["context:read"]);
    await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
    await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
    await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [job] }));
    await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({ json: { language: "en", segments: [{ evidence_id: "segment-1", speaker: "Speaker 1", start_seconds: 0, end_seconds: 1, text: "Transcript content." }], provider: "test", model: "test" } }));
    await page.route(`**/v1/conversations/${id}/summary`, (route) => route.fulfill({ json: summary }));

    await page.goto("/");
    await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
    await page.getByRole("tab", { name: "Summary" }).click();
    await expect(page.getByText(summary.overview.text)).toBeVisible();
    await expect(page.getByRole("alert").filter({ hasText: details })).toContainText(state === "failed" ? "The current summary is available, but the latest attempt failed." : "The current summary is available, but the latest attempt was cancelled.");
    await page.getByRole("tab", { name: "Status" }).click();
    const statusRow = page.locator(".status-row").filter({ hasText: "Summary" });
    await expect(statusRow.locator(".status-value")).toHaveText(state === "failed" ? "FAILED" : "CANCELLED");
    await expect(statusRow.getByRole("alert")).toContainText(details);
    await context.close();
  }
});

test("speaker mapping loads without rename access in the conversation heading and fits mobile", async ({ browser }) => {
  const id = "00000000-0000-0000-0000-000000000068";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z", true), people: ["Metadata person"], transcript_available: true };
  const mapping = {
    speaker_revision: 1,
    speakers: [
      { speaker_number: 1, label: "Speaker 1", display_name: "Jordan Lee with a full speaker name" },
      { speaker_number: 2, label: "Speaker 2", display_name: null },
      { speaker_number: 3, label: "Speaker 3", display_name: "Taylor with an extended display name" },
      { speaker_number: 4, label: "Speaker 4", display_name: "Casey with a full display name" },
    ],
    summary_job: null,
  };
  const transcript = {
    language: "en" as const,
    segments: [
      { evidence_id: "speaker-1", speaker: "Speaker 1", start_seconds: 0, end_seconds: 1, text: "First speaker." },
      { evidence_id: "speaker-2", speaker: "Speaker 2", start_seconds: 1, end_seconds: 2, text: "Second speaker." },
    ],
    provider: "test",
    model: "test",
  };
  let releaseMapping: () => void = () => undefined;
  const mappingGate = new Promise<void>((resolve) => { releaseMapping = resolve; });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  await installLocalAuth(page, ["context:read"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({ json: transcript }));
  await page.route(`**/v1/conversations/${id}/speakers`, async (route) => {
    await mappingGate;
    await route.fulfill({ json: mapping });
  });

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  await page.getByRole("tab", { name: "Transcript" }).click();
  await expect(page.getByText("Loading speaker names…")).toBeVisible();
  releaseMapping();
  const speakerList = page.locator(".conversation-review .detail-heading").getByRole("list", { name: "Conversation speakers" });
  await expect(page.locator(".app-header").getByRole("list", { name: "Conversation speakers" })).toHaveCount(0);
  await expect(speakerList.locator("li")).toHaveText([
    "Jordan Lee with a full speaker name",
    "Speaker 2",
    "Taylor with an extended display name",
    "Casey with a full display name",
  ]);
  await expect(page.locator(".people-list").getByText("Metadata person", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit speaker name for Speaker 1" })).toHaveCount(0);
  await expect(page.locator(".transcript-section").getByText("Jordan Lee with a full speaker name", { exact: true })).toBeVisible();
  await expect(page.locator(".transcript-section").getByText("Speaker 2", { exact: true })).toBeVisible();
  const firstPillStyles = await speakerList.locator("li").first().evaluate((element) => {
    const styles = getComputedStyle(element);
    return { maxWidth: styles.maxWidth, overflow: styles.overflow, textOverflow: styles.textOverflow };
  });
  expect(firstPillStyles).toEqual({ maxWidth: "none", overflow: "visible", textOverflow: "clip" });
  await expect(speakerList).toHaveAttribute("tabindex", "0");
  const dimensions = await speakerList.evaluate((element) => ({ clientWidth: element.clientWidth, scrollWidth: element.scrollWidth }));
  expect(dimensions.scrollWidth).toBeGreaterThan(dimensions.clientWidth);
  await speakerList.focus();
  await expect(speakerList).toBeFocused();
  await speakerList.evaluate((element) => { element.scrollLeft = element.scrollWidth; });
  const pills = await speakerList.boundingBox();
  const lastPill = await speakerList.locator("li").last().boundingBox();
  expect(pills).not.toBeNull();
  expect(lastPill).not.toBeNull();
  expect(pills!.x + pills!.width).toBeLessThanOrEqual(390);
  const scrollOffset = await speakerList.evaluate((element) => element.scrollLeft);
  expect(scrollOffset).toBeGreaterThan(0);
  expect(lastPill!.x).toBeLessThan(pills!.x + pills!.width);
  expect(lastPill!.x + lastPill!.width).toBeLessThanOrEqual(pills!.x + pills!.width + 1);
  await expect(speakerList.locator("li").last()).toHaveText("Casey with a full display name");
  await context.close();
});

test("speaker rename restores keyboard focus after save, cancel, and Escape", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000074";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z", true), transcript_available: true };
  const transcript = {
    language: "en" as const,
    segments: [{ evidence_id: "speaker-1", speaker: "Speaker 1", start_seconds: 0, end_seconds: 1, text: "Transcript content." }],
    provider: "test",
    model: "test",
  };
  let mapping = {
    speaker_revision: 1,
    speakers: [{ speaker_number: 1, label: "Speaker 1", display_name: null as string | null }],
    summary_job: null,
  };
  await installLocalAuth(page, ["context:read", "metadata:write"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({ json: transcript }));
  await page.route(`**/v1/conversations/${id}/speakers`, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: mapping });
      return;
    }
    mapping = { ...mapping, speaker_revision: mapping.speaker_revision + 1, speakers: [{ speaker_number: 1, label: "Speaker 1", display_name: route.request().postDataJSON().display_name }] };
    await route.fulfill({ status: 202, json: mapping });
  });

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  await page.getByRole("tab", { name: "Transcript" }).click();
  const editButton = page.getByRole("button", { name: "Edit speaker name for Speaker 1" });
  await editButton.click();
  await page.getByRole("textbox", { name: "Name for Speaker 1" }).fill("Morgan");
  await page.getByRole("button", { name: "Save name" }).click();
  await expect(editButton).toHaveText("Morgan");
  await expect(editButton).toBeFocused();

  await editButton.click();
  await page.keyboard.press("Escape");
  await expect(editButton).toBeFocused();

  await editButton.click();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(editButton).toBeFocused();
});

test("conversation tabs support arrow, Home, and End navigation to the speaker editor", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000075";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z", true), transcript_available: true };
  const transcript = {
    language: "en" as const,
    segments: [{ evidence_id: "speaker-1", speaker: "Speaker 1", start_seconds: 0, end_seconds: 1, text: "Transcript content." }],
    provider: "test",
    model: "test",
  };
  const mapping = {
    speaker_revision: 1,
    speakers: [{ speaker_number: 1, label: "Speaker 1", display_name: "Morgan" }],
    summary_job: null,
  };
  await installLocalAuth(page, ["context:read", "metadata:write"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({ json: transcript }));
  await page.route(`**/v1/conversations/${id}/speakers`, (route) => route.fulfill({ json: mapping }));

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  const tab = (name: string) => page.getByRole("tab", { name });
  await expect(page.getByRole("tab")).toHaveText(["Summary", "Transcript", "Notes", "Status"]);
  await expect(tab("Summary")).toHaveAttribute("aria-selected", "true");
  const summaryStatus = page.getByRole("region", { name: "Summary status" });
  await expect(summaryStatus.locator(".status-row")).toContainText("Summary");
  await expect(summaryStatus.locator(".status-value")).toHaveText("READY TO START");
  await tab("Summary").focus();
  await page.keyboard.press("ArrowRight");
  await expect(tab("Transcript")).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(tab("Notes")).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(tab("Status")).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(tab("Summary")).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(tab("Status")).toBeFocused();
  await page.keyboard.press("Home");
  await expect(tab("Summary")).toBeFocused();
  await page.keyboard.press("End");
  await expect(tab("Status")).toBeFocused();
  await tab("Transcript").click();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("tabpanel")).toBeFocused();
  await page.keyboard.press("Tab");
  const editButton = page.getByRole("button", { name: "Edit speaker name for Speaker 1" });
  await expect(editButton).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("textbox", { name: "Name for Speaker 1" })).toBeFocused();
});

test("selecting another conversation through navigation resets the active tab to Summary", async ({ page }) => {
  const firstId = "00000000-0000-0000-0000-000000000079";
  const secondId = "00000000-0000-0000-0000-000000000080";
  const first = { ...browserConversation(firstId, "2026-04-04T12:00:00Z", true), name: "First conversation" };
  const second = { ...browserConversation(secondId, "2026-04-04T12:01:00Z", true), name: "Second conversation" };
  await installLocalAuth(page);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [first, second] }));
  for (const id of [firstId, secondId]) {
    await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
    await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  }

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${first.name}` }).click();
  await expect(page.getByRole("tab", { name: "Summary" })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "Notes" }).click();
  await page.getByRole("link", { name: "Recordings" }).click();
  await page.getByRole("button", { name: `Open recording ${second.name}` }).click();
  await expect(page.getByRole("heading", { name: second.name })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Summary" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("region", { name: "Summary status" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Conversation speakers" })).toHaveCount(0);
});

test("speaker rename updates the transcript and conversation heading, then refreshes the summary", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000069";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z", true), transcript_available: true, summary_available: true };
  const transcript = {
    language: "en" as const,
    segments: [{ evidence_id: "speaker-1", speaker: "Speaker 1", start_seconds: 0, end_seconds: 1, text: "The transcript stays canonical." }],
    provider: "test",
    model: "test",
  };
  const originalSummary = {
    overview: { text: "The old summary is visible.", evidence_span_ids: ["speaker-1"], confidence: "certain" as const, owner: null, due_date: null },
    key_topics: [],
    decisions: [],
    action_items: [],
    provider: "test",
    model: "old",
  };
  const updatedSummary = { ...originalSummary, overview: { ...originalSummary.overview, text: "The refreshed summary is visible." }, model: "updated" };
  let mapping = {
    speaker_revision: 4,
    speakers: [{ speaker_number: 1, label: "Speaker 1", display_name: null as string | null }],
    summary_job: null as { id: string; kind: "summarization"; state: "queued" | "succeeded"; attempts: number; created_at: string; last_error_code: null; last_error_details: null } | null,
  };
  let patchBody: Record<string, unknown> | null = null;
  let patchIdempotencyKey: string | undefined;
  await installLocalAuth(page, ["context:read", "metadata:write"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: mapping.summary_job ? [mapping.summary_job] : [] }));
  await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({ json: transcript }));
  await page.route(`**/v1/conversations/${id}/summary`, (route) => route.fulfill({ json: mapping.summary_job?.state === "succeeded" ? updatedSummary : originalSummary }));
  await page.route(`**/v1/conversations/${id}/speakers`, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: mapping });
      return;
    }
    patchBody = route.request().postDataJSON() as Record<string, unknown>;
    patchIdempotencyKey = route.request().headers()["x-idempotency-key"];
    mapping = {
      ...mapping,
      speaker_revision: 5,
      speakers: [{ speaker_number: 1, label: "Speaker 1", display_name: "Morgan" }],
      summary_job: {
        id: "speaker-summary-job",
        kind: "summarization",
        state: "queued",
        attempts: 0,
        created_at: "2026-04-04T12:00:01Z",
        last_error_code: null,
        last_error_details: null,
      },
    };
    await route.fulfill({ status: 202, json: mapping });
  });

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  await page.getByRole("tab", { name: "Summary" }).click();
  await expect(page.getByText(originalSummary.overview.text)).toBeVisible();
  await page.getByRole("tab", { name: "Transcript" }).click();
  await page.getByRole("button", { name: "Edit speaker name for Speaker 1" }).click();
  await page.getByRole("textbox", { name: "Name for Speaker 1" }).fill("Morgan");
  await page.getByRole("button", { name: "Save name" }).click();

  const headingSpeakerList = page.locator(".conversation-review .detail-heading").getByRole("list", { name: "Conversation speakers" });
  await expect(headingSpeakerList.getByText("Morgan")).toBeVisible();
  await expect(page.locator(".app-header").getByRole("list", { name: "Conversation speakers" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Edit speaker name for Speaker 1" })).toHaveText("Morgan");
  await expect(page.getByText("The transcript stays canonical.")).toBeVisible();
  expect(patchBody).toEqual({ expected_speaker_revision: 4, speaker_number: 1, display_name: "Morgan" });
  expect(patchIdempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
  await page.getByRole("tab", { name: "Summary" }).click();
  await expect(page.getByRole("progressbar", { name: "Summary progress" })).toBeVisible();
  mapping.summary_job!.state = "succeeded";
  await expect(page.getByText(updatedSummary.overview.text)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(originalSummary.overview.text)).toHaveCount(0);
  mapping.summary_job!.state = "succeeded";
  await page.reload();
  const persistedSpeakerList = page.locator(".conversation-review .detail-heading").getByRole("list", { name: "Conversation speakers" });
  await expect(persistedSpeakerList.getByText("Morgan")).toBeVisible();
  await expect(page.locator(".app-header").getByRole("list", { name: "Conversation speakers" })).toHaveCount(0);
});

test("speaker rename handles revision conflicts and API failures without losing the draft", async ({ browser }) => {
  const id = "00000000-0000-0000-0000-000000000070";
  for (const outcome of ["conflict", "failure", "conflict-reload-failure"] as const) {
    const context = await browser.newContext();
    const page = await context.newPage();
    const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z", true), transcript_available: true };
    const transcript = {
      language: "en" as const,
      segments: [{ evidence_id: "speaker-1", speaker: "Speaker 1", start_seconds: 0, end_seconds: 1, text: "Current transcript." }],
      provider: "test",
      model: "test",
    };
    let mapping = {
      speaker_revision: 1,
      speakers: [{ speaker_number: 1, label: "Speaker 1", display_name: "Current name" as string | null }],
      summary_job: null,
    };
    let mappingRequests = 0;
    await installLocalAuth(page, ["context:read", "metadata:write"]);
    await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
    await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
    await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
    await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({ json: transcript }));
    await page.route(`**/v1/conversations/${id}/speakers`, async (route) => {
      if (route.request().method() === "GET") {
        mappingRequests += 1;
        if (outcome === "conflict-reload-failure" && mappingRequests > 1) {
          await route.fulfill({ status: 503, json: { error: { kind: "unavailable", message: "Speaker service is unavailable." } } });
          return;
        }
        await route.fulfill({ json: mapping });
        return;
      }
      if (outcome !== "failure") {
        mapping = { ...mapping, speaker_revision: 2, speakers: [{ speaker_number: 1, label: "Speaker 1", display_name: "External name" }] };
        await route.fulfill({ status: 409, json: { error: { kind: "conflict", message: "Speaker mapping revision changed." } } });
        return;
      }
      await route.fulfill({ status: 503, json: { error: { kind: "unavailable", message: "Speaker service is unavailable." } } });
    });

    await page.goto("/");
    await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
    await page.getByRole("tab", { name: "Transcript" }).click();
    await page.getByRole("button", { name: "Edit speaker name for Speaker 1" }).click();
    const input = page.getByRole("textbox", { name: "Name for Speaker 1" });
    await input.fill("Draft name");
    await page.getByRole("button", { name: "Save name" }).click();
    const expectedError = outcome === "conflict"
      ? "changed elsewhere"
      : outcome === "conflict-reload-failure"
        ? "could not be reloaded"
        : "Speaker service is unavailable";
    await expect(page.getByRole("alert").filter({ hasText: expectedError })).toBeVisible();
    await expect(input).toHaveValue("Draft name");
    if (outcome === "conflict") {
      await expect(page.getByRole("list", { name: "Conversation speakers" }).getByText("External name")).toBeVisible();
      expect(mappingRequests).toBeGreaterThan(1);
    } else {
      await expect(page.getByRole("list", { name: "Conversation speakers" }).getByText("Current name")).toBeVisible();
      if (outcome === "conflict-reload-failure") {
        expect(mappingRequests).toBeGreaterThan(1);
        await expect(page.getByRole("alert").filter({ hasText: "Current names have been reloaded" })).toHaveCount(0);
      }
    }
    await page.getByRole("button", { name: "Cancel" }).click();
    await context.close();
  }
});

test("speaker edits become disabled when transcription starts", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000071";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z", true), transcript_available: true };
  const transcript = {
    language: "en" as const,
    segments: [{ evidence_id: "speaker-1", speaker: "Speaker 1", start_seconds: 0, end_seconds: 1, text: "Existing transcript." }],
    provider: "test",
    model: "test",
  };
  const mapping = { speaker_revision: 1, speakers: [{ speaker_number: 1, label: "Speaker 1", display_name: null }], summary_job: null };
  let jobState: "queued" | "running" = "queued";
  let patchRequests = 0;
  await installLocalAuth(page, ["context:read", "metadata:write"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: jobState === "queued" ? [] : [{ id: "transcription-job", kind: "transcription", state: jobState, attempts: 1, created_at: "2026-04-04T12:00:01Z", last_error_code: null, last_error_details: null }] }));
  await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({ json: transcript }));
  await page.route(`**/v1/conversations/${id}/speakers`, (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: mapping });
    patchRequests += 1;
    return route.fulfill({ status: 202, json: mapping });
  });

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  await page.getByRole("tab", { name: "Transcript" }).click();
  await page.getByRole("button", { name: "Edit speaker name for Speaker 1" }).click();
  const input = page.getByRole("textbox", { name: "Name for Speaker 1" });
  await input.fill("New name");
  jobState = "running";
  await expect(input).toBeDisabled({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: "Save name" })).toBeDisabled();
  await expect(page.getByRole("status").filter({ hasText: "Speaker names cannot be changed while transcription is active." })).toBeVisible();
  expect(patchRequests).toBe(0);
});

test("speaker mapping responses from a previous conversation do not replace the current heading", async ({ page }) => {
  const firstId = "00000000-0000-0000-0000-000000000072";
  const secondId = "00000000-0000-0000-0000-000000000073";
  const conversations = [
    { ...browserConversation(firstId, "2026-04-04T12:00:00Z", true), name: "First recording", transcript_available: true },
    { ...browserConversation(secondId, "2026-04-04T12:00:00Z", true), name: "Second recording", transcript_available: true },
  ];
  let releaseFirstMapping: () => void = () => undefined;
  const firstMappingGate = new Promise<void>((resolve) => { releaseFirstMapping = resolve; });
  await installLocalAuth(page, ["context:read", "metadata:write"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: conversations }));
  for (const id of [firstId, secondId]) {
    await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
    await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
    await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({ json: { language: "en", segments: [{ evidence_id: "segment", speaker: "Speaker 1", start_seconds: 0, end_seconds: 1, text: `${id} transcript.` }], provider: "test", model: "test" } }));
    await page.route(`**/v1/conversations/${id}/speakers`, async (route) => {
      if (id === firstId) await firstMappingGate;
      await route.fulfill({ json: { speaker_revision: 1, speakers: [{ speaker_number: 1, label: "Speaker 1", display_name: id === firstId ? "First speaker" : "Second speaker" }], summary_job: null } });
    });
  }

  await page.goto("/");
  await page.getByRole("button", { name: "Open recording First recording" }).click();
  await page.getByRole("tab", { name: "Transcript" }).click();
  await expect(page.getByText("Loading speaker names…")).toBeVisible();
  await page.getByRole("navigation", { name: "Breadcrumb" }).getByRole("link", { name: "Recordings" }).click();
  await page.getByRole("button", { name: "Open recording Second recording" }).click();
  await expect(page.getByRole("list", { name: "Conversation speakers" }).getByText("Second speaker")).toBeVisible();
  releaseFirstMapping();
  await expect(page.getByRole("list", { name: "Conversation speakers" }).getByText("First speaker")).toHaveCount(0);
  await expect(page.getByText(`${firstId} transcript.`)).toHaveCount(0);
});

test("transcription retry is available from every tab, preserves the current transcript, and refreshes it on success", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000061";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z", true), transcript_available: true };
  const previousTranscript = {
    language: "en" as const,
    segments: [{ evidence_id: "old-segment", speaker: "Speaker 1", start_seconds: 0, end_seconds: 1, text: "The existing transcript stays available." }],
    provider: "test",
    model: "previous",
  };
  const retriedTranscript = {
    ...previousTranscript,
    segments: [{ evidence_id: "new-segment", speaker: "Speaker 1", start_seconds: 0, end_seconds: 1, text: "The retried transcript is loaded." }],
    model: "retried",
  };
  let job = {
    id: "transcription-job",
    kind: "transcription",
    state: "failed",
    attempts: 1,
    created_at: "2026-04-04T12:00:01Z",
    last_error_code: "provider_error",
    last_error_details: "The transcription provider failed.",
  };
  let stageRequests = 0;
  let stageKey: string | undefined;
  let releaseStage: () => void = () => undefined;
  let reportStageRequest: () => void = () => undefined;
  const stageGate = new Promise<void>((resolve) => { releaseStage = resolve; });
  const stageRequest = new Promise<void>((resolve) => { reportStageRequest = resolve; });
  await installLocalAuth(page, ["processing:admin"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [job] }));
  await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({ json: job.state === "succeeded" ? retriedTranscript : previousTranscript }));
  await page.route(`**/v1/conversations/${id}/jobs/transcription`, async (route) => {
    stageRequests += 1;
    stageKey = route.request().headers()["x-idempotency-key"];
    reportStageRequest();
    await stageGate;
    job = { ...job, state: "queued", attempts: 1 };
    await route.fulfill({ status: 202, json: job });
  });

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  await page.getByRole("tab", { name: "Transcript" }).click();
  await expect(page.getByText(previousTranscript.segments[0]!.text)).toBeVisible();
  for (const tab of ["Status", "Notes", "Transcript", "Summary"]) {
    await page.getByRole("tab", { name: tab }).click();
    await page.getByRole("button", { name: "Conversation actions" }).click();
    await expect(page.getByRole("menuitem", { name: "Retry transcription" })).toBeVisible();
    await page.keyboard.press("Escape");
  }

  const trigger = page.getByRole("button", { name: "Conversation actions" });
  await trigger.click();
  await page.getByRole("menuitem", { name: "Retry transcription" }).click();
  await stageRequest;
  expect(stageRequests).toBe(1);
  expect(stageKey).toMatch(/^[0-9a-f-]{36}$/);
  await trigger.click();
  await expect(page.getByRole("menuitem", { name: "Queueing transcription…" })).toBeDisabled();
  releaseStage();
  const queuedAction = page.getByRole("menuitem", { name: "Transcription queued" });
  await expect(queuedAction).toBeDisabled();
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: "Transcript" }).click();
  await expect(page.getByText(previousTranscript.segments[0]!.text)).toBeVisible();

  job = { ...job, state: "running" };
  await trigger.click();
  await expect(page.getByRole("menuitem", { name: "Transcribing…" })).toBeDisabled({ timeout: 10_000 });
  await page.keyboard.press("Escape");
  job = { ...job, state: "succeeded" };
  await expect(page.getByText(retriedTranscript.segments[0]!.text)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(previousTranscript.segments[0]!.text)).toHaveCount(0);
  await trigger.click();
  await expect(page.getByRole("menuitem", { name: "Retry transcription" })).toHaveCount(0);
  expect(stageRequests).toBe(1);
});

test("transcription retry requires permission and a recording, and offers cancelled jobs with status feedback", async ({ browser }) => {
  const id = "00000000-0000-0000-0000-000000000062";
  const cases: Array<{ capabilities: Capability[]; recordingAvailable: boolean; transcriptAvailable?: boolean; jobState: string; expectedGear: boolean; expectedRetry: boolean; expectedStatus?: string }> = [
    { capabilities: [], recordingAvailable: true, jobState: "failed", expectedGear: false, expectedRetry: false },
    { capabilities: ["processing:admin"], recordingAvailable: false, jobState: "failed", expectedGear: true, expectedRetry: false },
    { capabilities: ["processing:admin"], recordingAvailable: true, jobState: "running", expectedGear: true, expectedRetry: false },
    { capabilities: ["processing:admin"], recordingAvailable: true, jobState: "succeeded", expectedGear: true, expectedRetry: false },
    { capabilities: ["processing:admin"], recordingAvailable: true, transcriptAvailable: true, jobState: "cancelled", expectedGear: true, expectedRetry: true, expectedStatus: "CANCELLED" },
  ];
  for (const capabilityCase of cases) {
    const context = await browser.newContext();
    const page = await context.newPage();
    const conversation = {
      ...browserConversation(id, "2026-04-04T12:00:00Z", capabilityCase.recordingAvailable),
      transcript_available: capabilityCase.transcriptAvailable ?? false,
    };
    const job = {
      id: "transcription-job",
      kind: "transcription",
      state: capabilityCase.jobState,
      attempts: 1,
      created_at: "2026-04-04T12:00:01Z",
      last_error_code: null,
      last_error_details: null,
    };
    await installLocalAuth(page, capabilityCase.capabilities);
    await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
    await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
    await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [job] }));
    await page.goto("/");
    await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
    const trigger = page.getByRole("button", { name: "Conversation actions" });
    await expect(trigger).toHaveCount(Number(capabilityCase.expectedGear));
    if (capabilityCase.expectedStatus) {
      await page.getByRole("tab", { name: "Status" }).click();
      const transcriptionRow = page.locator(".status-row").filter({ hasText: "Transcription" });
      await expect(transcriptionRow.locator(".status-value")).toHaveText(capabilityCase.expectedStatus);
      await expect(transcriptionRow.getByRole("alert")).toContainText("latest attempt was cancelled");
    }
    if (capabilityCase.expectedGear) {
      await trigger.click();
      const retryAction = page.getByRole("menuitem", { name: "Retry transcription" });
      await expect(retryAction).toHaveCount(Number(capabilityCase.expectedRetry));
      if (capabilityCase.expectedRetry) await expect(retryAction).toBeEnabled();
    }
    await context.close();
  }
});

test("transcription staging failures stay visible on every tab and allow another retry", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000063";
  const conversation = browserConversation(id, "2026-04-04T12:00:00Z", true);
  let stageRequests = 0;
  await installLocalAuth(page, ["processing:admin"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs/transcription`, (route) => {
    stageRequests += 1;
    return route.fulfill({ status: 503, json: { error: { kind: "service_unavailable", message: "Transcription staging is temporarily unavailable." } } });
  });

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  const error = page.locator(".conversation-action-error");
  await expect(error).toHaveText("Transcription staging is temporarily unavailable.");
  expect(stageRequests).toBe(1);
  await page.getByRole("tab", { name: "Notes" }).click();
  await expect(error).toBeVisible();
  const trigger = page.getByRole("button", { name: "Conversation actions" });
  await trigger.click();
  await expect(page.getByRole("menuitem", { name: "Retry transcription" })).toBeEnabled();
  await page.getByRole("menuitem", { name: "Retry transcription" }).click();
  await expect(error).toHaveText("Transcription staging is temporarily unavailable.");
  expect(stageRequests).toBe(2);
  await trigger.click();
  await expect(page.getByRole("menuitem", { name: "Retry transcription" })).toBeEnabled();
});

test("re-summarize queues once, preserves the current summary, and refreshes it when processing completes", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000055";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z"), transcript_available: true, summary_available: true };
  const previousSummary = {
    overview: { text: "The existing summary stays visible while re-summarization runs.", evidence_span_ids: ["segment-1"], confidence: "certain" as const, owner: null, due_date: null },
    key_topics: [],
    decisions: [],
    action_items: [],
    provider: "test",
    model: "previous",
  };
  const refreshedSummary = {
    ...previousSummary,
    overview: { ...previousSummary.overview, text: "The updated summary is loaded after completion." },
    model: "updated",
  };
  let job: { id: string; kind: string; state: string; attempts: number; created_at: string; last_error_code: null; last_error_details: null } | null = null;
  let stageRequests = 0;
  let stageKey: string | undefined;
  let summaryRequests = 0;
  let releaseStage: () => void = () => undefined;
  let reportStageRequest: () => void = () => undefined;
  const stageGate = new Promise<void>((resolve) => { releaseStage = resolve; });
  const stageRequest = new Promise<void>((resolve) => { reportStageRequest = resolve; });
  await installLocalAuth(page, ["processing:admin"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: job ? [job] : [] }));
  await page.route(`**/v1/conversations/${id}/jobs/summarization`, async (route) => {
    stageRequests += 1;
    stageKey = route.request().headers()["x-idempotency-key"];
    reportStageRequest();
    await stageGate;
    job = { id: "summary-job", kind: "summarization", state: "queued", attempts: 0, created_at: "2026-04-04T12:00:01Z", last_error_code: null, last_error_details: null };
    await route.fulfill({ status: 202, json: job });
  });
  await page.route(`**/v1/conversations/${id}/summary`, (route) => {
    summaryRequests += 1;
    return route.fulfill({ json: summaryRequests === 1 ? previousSummary : refreshedSummary });
  });

  await page.goto(`/recordings/${id}`);
  await page.getByRole("tab", { name: "Summary" }).click();
  await expect(page.getByText(previousSummary.overview.text)).toBeVisible();
  const trigger = page.getByRole("button", { name: "Conversation actions" });
  await trigger.click();
  const menu = page.getByRole("menu", { name: "Conversation actions" });
  await menu.getByRole("menuitem", { name: "Re-summarize" }).click();
  await stageRequest;
  expect(stageRequests).toBe(1);
  expect(stageKey).toMatch(/^[0-9a-f-]{36}$/);

  await trigger.click();
  const stagingAction = menu.getByRole("menuitem", { name: "Queueing re-summarization…" });
  await expect(stagingAction).toBeDisabled();
  releaseStage();
  const queuedAction = menu.getByRole("menuitem", { name: "Summarization queued" });
  await expect(queuedAction).toBeDisabled();
  await expect(page.getByText(previousSummary.overview.text)).toBeVisible();
  await page.keyboard.press("Escape");

  await page.getByRole("tab", { name: "Status" }).click();
  const summaryRow = page.locator(".status-row").filter({ hasText: "Summary" });
  await expect(summaryRow.getByText("STAGED")).toBeVisible();
  await page.getByRole("tab", { name: "Summary" }).click();
  await expect(page.getByText(previousSummary.overview.text)).toBeVisible();

  job = { ...job!, state: "succeeded" };
  await expect(page.getByText(refreshedSummary.overview.text)).toBeVisible({ timeout: 10_000 });
  expect(summaryRequests).toBeGreaterThan(1);
  expect(stageRequests).toBe(1);
});

test("a delayed initial summary response cannot replace the completed summary", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000058";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z"), transcript_available: true, summary_available: true };
  const delayedSummary = {
    overview: { text: "A delayed initial summary.", evidence_span_ids: [], confidence: "certain" as const, owner: null, due_date: null },
    key_topics: [],
    decisions: [],
    action_items: [],
    provider: "test",
    model: "initial",
  };
  const completedSummary = {
    ...delayedSummary,
    overview: { ...delayedSummary.overview, text: "The completed summary stays current." },
    model: "completed",
  };
  const completedJob = {
    id: "summary-job",
    kind: "summarization",
    state: "succeeded",
    attempts: 1,
    created_at: "2026-04-04T12:00:01Z",
    last_error_code: null,
    last_error_details: null,
  };
  let summaryRequests = 0;
  let reportInitialRequest: () => void = () => undefined;
  let releaseInitialSummary: () => void = () => undefined;
  let reportInitialResponse: () => void = () => undefined;
  const initialRequest = new Promise<void>((resolve) => { reportInitialRequest = resolve; });
  const initialGate = new Promise<void>((resolve) => { releaseInitialSummary = resolve; });
  const initialResponse = new Promise<void>((resolve) => { reportInitialResponse = resolve; });
  await installLocalAuth(page, ["processing:admin"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [completedJob] }));
  await page.route(`**/v1/conversations/${id}/summary`, async (route) => {
    summaryRequests += 1;
    if (summaryRequests === 1) {
      reportInitialRequest();
      await initialGate;
      await route.fulfill({ json: delayedSummary });
      reportInitialResponse();
      return;
    }
    await route.fulfill({ json: completedSummary });
  });

  await page.goto(`/recordings/${id}`);
  await initialRequest;
  await expect.poll(() => summaryRequests).toBe(2);
  await page.getByRole("tab", { name: "Summary" }).click();
  await expect(page.getByText(completedSummary.overview.text)).toBeVisible();
  releaseInitialSummary();
  await initialResponse;
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.getByText(completedSummary.overview.text)).toBeVisible();
  await expect(page.getByText(delayedSummary.overview.text)).toHaveCount(0);
});

test("re-summarize requires processing capability and a current transcript", async ({ browser }) => {
  const id = "00000000-0000-0000-0000-000000000056";
  const cases: Array<{ capabilities: Capability[]; transcriptAvailable: boolean; expectedAction: boolean; enabled: boolean }> = [
    { capabilities: [], transcriptAvailable: true, expectedAction: false, enabled: false },
    { capabilities: ["processing:admin"], transcriptAvailable: false, expectedAction: true, enabled: false },
  ];
  for (const capabilityCase of cases) {
    const context = await browser.newContext();
    const page = await context.newPage();
    const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z"), transcript_available: capabilityCase.transcriptAvailable };
    await installLocalAuth(page, capabilityCase.capabilities);
    await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
    await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
    await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
    await page.goto(`/recordings/${id}`);
    const trigger = page.getByRole("button", { name: "Conversation actions" });
    await expect(trigger).toHaveCount(Number(capabilityCase.expectedAction));
    if (capabilityCase.expectedAction) {
      await trigger.click();
      const action = page.getByRole("menuitem", { name: "Re-summarize" });
      await expect(action).toBeVisible();
      await expect(action).toHaveAttribute("title", "A current transcript is required to re-summarize.");
      await expect(action).toBeDisabled();
    }
    await context.close();
  }
});

test("re-summarize displays the API failure and allows a retry", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000057";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z"), transcript_available: true, summary_available: true };
  const summary = {
    overview: { text: "The current summary remains available.", evidence_span_ids: [], confidence: "certain" as const, owner: null, due_date: null },
    key_topics: [],
    decisions: [],
    action_items: [],
    provider: "test",
    model: "test",
  };
  let stageRequests = 0;
  await installLocalAuth(page, ["processing:admin"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/speakers`, (route) => route.fulfill({ json: { speaker_revision: 1, speakers: [], summary_job: null } }));
  await page.route(`**/v1/conversations/${id}/summary`, (route) => route.fulfill({ json: summary }));
  await page.route(`**/v1/conversations/${id}/jobs/summarization`, (route) => {
    stageRequests += 1;
    return route.fulfill({ status: 503, json: { error: { kind: "service_unavailable", message: "The summary provider is temporarily unavailable." } } });
  });

  await page.goto(`/recordings/${id}`);
  await page.getByRole("tab", { name: "Summary" }).click();
  await expect(page.getByText(summary.overview.text)).toBeVisible();
  await page.getByRole("button", { name: "Conversation actions" }).click();
  await page.getByRole("menuitem", { name: "Re-summarize" }).click();
  await expect(page.getByRole("button", { name: "Conversation actions" })).toBeFocused();
  await expect(page.locator(".conversation-action-error")).toHaveCount(1);
  await expect(page.locator(".conversation-action-error")).toHaveText("The summary provider is temporarily unavailable.");
  await expect(page.getByText(summary.overview.text)).toBeVisible();
  await page.getByRole("button", { name: "Conversation actions" }).click();
  await expect(page.getByRole("menuitem", { name: "Re-summarize" })).toBeEnabled();
  expect(stageRequests).toBe(1);
});

test("a delayed jobs poll cannot overwrite a newer re-summarize result", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000060";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z"), transcript_available: true };
  const staleQueuedJob = {
    id: "summary-job",
    kind: "summarization",
    state: "queued",
    attempts: 0,
    created_at: "2026-04-04T12:00:01Z",
    last_error_code: null,
    last_error_details: null,
  };
  const failedJob = { ...staleQueuedJob, state: "failed", last_error_details: "The summary provider failed." };
  let jobPollRequests = 0;
  let reportPollStarted: () => void = () => undefined;
  let releasePoll: () => void = () => undefined;
  let reportPollResponse: () => void = () => undefined;
  const pollStarted = new Promise<void>((resolve) => { reportPollStarted = resolve; });
  const pollGate = new Promise<void>((resolve) => { releasePoll = resolve; });
  const pollResponse = new Promise<void>((resolve) => { reportPollResponse = resolve; });
  await page.clock.install();
  await installLocalAuth(page, ["processing:admin"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, async (route) => {
    jobPollRequests += 1;
    reportPollStarted();
    await pollGate;
    await route.fulfill({ json: [staleQueuedJob] });
    reportPollResponse();
  });
  await page.route(`**/v1/conversations/${id}/jobs/summarization`, (route) => route.fulfill({ status: 202, json: failedJob }));

  await page.goto(`/recordings/${id}`);
  await pollStarted;
  const trigger = page.getByRole("button", { name: "Conversation actions" });
  await trigger.click();
  await page.getByRole("menuitem", { name: "Re-summarize" }).click();
  await trigger.click();
  const retryAction = page.getByRole("menuitem", { name: "Re-summarize" });
  await expect(retryAction).toBeEnabled();
  await page.clock.fastForward(15_000);
  expect(jobPollRequests).toBe(1);
  releasePoll();
  await pollResponse;
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(retryAction).toBeEnabled();
  await expect(page.getByRole("menuitem", { name: "Summarization queued" })).toHaveCount(0);
});

test("conversation review shows unavailable instead of CHECKING when the jobs API is missing", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000007";
  const conversation = browserConversation(id, "2026-01-01T00:00:00Z", true);
  let stageRequested = false;
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}`, (route) => route.fulfill({ json: conversation }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({
    status: 404,
    json: { error: { kind: "resource_not_found", message: "Route not found" } },
  }));
  await page.route(`**/v1/conversations/${id}/jobs/transcription`, async (route) => {
    stageRequested = true;
    await route.fulfill({ status: 202, json: {} });
  });

  await page.goto("/");
  await page.getByRole("button", { name: `Open recording ${conversation.name}` }).click();
  await page.getByRole("tab", { name: "Status" }).click();

  const transcriptionStatus = page.locator(".status-row").filter({ hasText: "Transcription" });
  await expect(transcriptionStatus).toContainText("UNAVAILABLE");
  await expect(transcriptionStatus.getByRole("alert").filter({ hasText: "Job status is unavailable" })).toBeVisible();
  expect(stageRequested).toBe(false);
});

test("global breadcrumbs update with metadata and remain usable on narrow screens", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000043";
  let conversation = { ...browserConversation(id, "2026-04-01T12:00:00Z", true), name: "Accessible review" };
  await installLocalAuth(page);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}`, async (route) => {
    if (route.request().method() !== "PATCH") {
      await route.continue();
      return;
    }
    const changes = route.request().postDataJSON() as { name: string };
    conversation = { ...conversation, name: changes.name, revision: conversation.revision + 1 };
    await route.fulfill({ json: conversation });
  });
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => route.fulfill({ json: { grant: "heading-grant" } }));
  await page.route(`**/v1/conversations/${id}/recording?grant=**`, (route) => fulfillAudioStream(route, waveFile()));

  await page.goto(`/recordings/${id}`);
  const breadcrumb = page.locator(".app-title-bar").getByRole("navigation", { name: "Breadcrumb" });
  await expect(breadcrumb.getByRole("link", { name: "Recordings" })).toHaveAttribute("href", "/recordings");
  await expect(breadcrumb.getByText("Accessible review")).toHaveAttribute("aria-current", "page");
  await expect(page.locator(".conversation-review nav[aria-label='Breadcrumb']")).toHaveCount(0);
  await expect(page.getByRole("heading", { level: 1, name: "Accessible review" })).toBeVisible();
  const trigger = page.getByRole("button", { name: "Conversation actions" });
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(trigger).not.toHaveAttribute("aria-controls");
  for (const tab of ["Status", "Notes", "Transcript", "Summary"]) {
    await page.getByRole("tab", { name: tab }).click();
    await trigger.click();
    await expect(trigger).toHaveAttribute("aria-controls", "conversation-actions-menu");
    await expect(page.getByRole("menu", { name: "Conversation actions" })).toHaveAttribute("id", "conversation-actions-menu");
    await expect(page.getByRole("menuitem", { name: "Edit details" })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: "Delete conversation" })).toBeVisible();
    const downloads = await openDownloadSubmenu(page);
    await expect(downloads.getByRole("menuitem", { name: "Download recording" })).toBeVisible();
    await expect(page.locator(".conversation-title-line .conversation-actions-menu")).toBeVisible();
    await page.keyboard.press("Escape");
  }

  await trigger.click();
  const editAction = page.getByRole("menuitem", { name: "Edit details" });
  const deleteAction = page.getByRole("menuitem", { name: "Delete conversation" });
  await expect(editAction).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(deleteAction).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await trigger.click();
  await page.getByRole("menuitem", { name: "Edit details" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Title").fill("Renamed review");
  await dialog.getByRole("button", { name: "Save changes" }).click();
  await expect(dialog).toBeHidden();
  await expect(breadcrumb.getByText("Renamed review")).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { level: 1, name: "Renamed review" })).toBeVisible();

  await page.setViewportSize({ width: 320, height: 700 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await expect(breadcrumb).toBeVisible();
  await expect(breadcrumb.getByText("Renamed review")).toBeVisible();
  await trigger.click();
  const menuBox = await page.getByRole("menu", { name: "Conversation actions" }).boundingBox();
  expect(menuBox?.x).toBeGreaterThanOrEqual(0);
  expect(menuBox ? menuBox.x + menuBox.width : Infinity).toBeLessThanOrEqual(320);
  const downloads = await openDownloadSubmenu(page);
  const downloadBox = await downloads.boundingBox();
  expect(downloadBox?.x).toBeGreaterThanOrEqual(0);
  expect(downloadBox ? downloadBox.x + downloadBox.width : Infinity).toBeLessThanOrEqual(320);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(page.locator(":focus")).not.toHaveCount(0);
  await breadcrumb.getByRole("link", { name: "Recordings" }).click();
  await expect(page).toHaveURL(/\/recordings$/);
  await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toHaveCount(0);
  await page.getByRole("button", { name: "Open recording Renamed review" }).click();
  await expect(breadcrumb.getByText("Renamed review")).toHaveAttribute("aria-current", "page");
});

test("direct detail navigation shows loading and failure breadcrumbs", async ({ page }) => {
  const loadedId = "00000000-0000-0000-0000-000000000047";
  const failedId = "00000000-0000-0000-0000-000000000048";
  const conversation = { ...browserConversation(loadedId, "2026-04-01T12:00:00Z"), name: "Loaded directly" };
  const recoveredConversation = { ...browserConversation(failedId, "2026-04-02T12:00:00Z"), name: "Recovered recording" };
  const loadGate: { finish?: () => void } = {};
  const retryGate: { finish?: () => void } = {};
  let retryLoad = false;
  let failedLoadAttempts = 0;
  await installLocalAuth(page);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${loadedId}`, async (route) => {
    await new Promise<void>((resolve) => { loadGate.finish = resolve; });
    await route.fulfill({ json: conversation });
  });
  await page.route(`**/v1/conversations/${failedId}`, async (route) => {
    failedLoadAttempts += 1;
    if (!retryLoad) {
      await route.fulfill({ status: 404, json: { error: { kind: "resource_not_found", message: "Not found" } } });
      return;
    }
    await new Promise<void>((resolve) => { retryGate.finish = resolve; });
    await route.fulfill({ json: recoveredConversation });
  });
  await page.route("**/v1/conversations/*/notes", (route) => route.fulfill({ json: [] }));
  await page.route("**/v1/conversations/*/jobs", (route) => route.fulfill({ json: [] }));

  await page.goto(`/recordings/${loadedId}`);
  const breadcrumb = page.locator(".app-header").getByRole("navigation", { name: "Breadcrumb" });
  await expect(breadcrumb.getByRole("link", { name: "Recordings" })).toBeVisible();
  await expect(breadcrumb.getByText("Loading recording…")).toHaveAttribute("aria-current", "page");
  await expect(page.locator(".conversation-review nav[aria-label='Breadcrumb']")).toHaveCount(0);
  await expect.poll(() => loadGate.finish !== undefined).toBe(true);
  loadGate.finish?.();
  await expect(breadcrumb.getByText("Loaded directly")).toHaveAttribute("aria-current", "page");

  await page.goto(`/recordings/${failedId}`);
  const failedBreadcrumb = page.locator(".app-header").getByRole("navigation", { name: "Breadcrumb" });
  await expect(failedBreadcrumb.getByText("Recording unavailable")).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("alert").filter({ hasText: "This recording could not be loaded." })).toBeVisible();
  const failedLoadsBeforeRetry = failedLoadAttempts;
  await failedBreadcrumb.getByRole("link", { name: "Recordings" }).click();
  await expect(page).toHaveURL(/\/recordings$/);
  retryLoad = true;
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/recordings/${failedId}$`));
  await expect.poll(() => failedLoadAttempts).toBeGreaterThan(failedLoadsBeforeRetry);
  await expect(failedBreadcrumb.getByText("Loading recording…")).toHaveAttribute("aria-current", "page");
  await expect(failedBreadcrumb.getByText("Recording unavailable")).toHaveCount(0);
  retryGate.finish?.();
  await expect(failedBreadcrumb.getByText("Recovered recording")).toHaveAttribute("aria-current", "page");
});

test("conversation review remains page-scrollable on short landscape screens", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000046";
  const conversation = {
    ...browserConversation(id, "2026-04-01T12:00:00Z", true),
    summary_available: true,
  };
  const summaryText = "A detailed finding remains reachable in the review. ".repeat(80);
  await installLocalAuth(page, ["context:read"]);
  await page.setViewportSize({ width: 844, height: 390 });
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => route.fulfill({ json: { grant: "landscape-grant" } }));
  await page.route(`**/v1/conversations/${id}/recording?grant=**`, (route) => fulfillAudioStream(route, waveFile()));
  await page.route(`**/v1/conversations/${id}/summary`, (route) => route.fulfill({
    json: {
      overview: { text: summaryText, evidence_span_ids: [], confidence: "certain", owner: null, due_date: null },
      key_topics: [],
      decisions: [],
      action_items: [],
      provider: "test",
      model: "test",
    },
  }));

  await page.goto(`/recordings/${id}`);
  await page.getByRole("tab", { name: "Summary" }).click();
  const summary = page.locator(".summary-group p");
  await expect(summary).toHaveText(summaryText);
  const layout = await page.evaluate(() => ({
    pageHeight: document.documentElement.scrollHeight,
    viewportHeight: window.innerHeight,
    panelOverflow: (() => {
      const panel = document.querySelector(".review-panel");
      return panel ? getComputedStyle(panel).overflowY : "missing";
    })(),
  }));
  expect(layout.pageHeight).toBeGreaterThan(layout.viewportHeight);
  expect(layout.panelOverflow).toBe("visible");
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect(summary).toBeInViewport();
  await expect(page.getByRole("tab", { name: "Status" })).toBeVisible();
});

test("conversation action menu honors independent capabilities and derivative availability", async ({ browser }) => {
  const id = "00000000-0000-0000-0000-000000000044";
  const cases: Array<{ capabilities: Capability[]; edit: boolean; remove: boolean; download: boolean }> = [
    { capabilities: ["metadata:write"], edit: true, remove: false, download: false },
    { capabilities: ["deletion:write"], edit: false, remove: true, download: false },
    { capabilities: ["context:read"], edit: false, remove: false, download: true },
    { capabilities: [], edit: false, remove: false, download: false },
  ];
  for (const capabilityCase of cases) {
    const context = await browser.newContext();
    const page = await context.newPage();
    const conversation = browserConversation(id, "2026-04-01T12:00:00Z");
    conversation.transcript_available = capabilityCase.download;
    conversation.summary_available = capabilityCase.download;
    await installLocalAuth(page, capabilityCase.capabilities);
    await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
    await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
    await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
    await page.goto(`/recordings/${id}`);
    const trigger = page.getByRole("button", { name: "Conversation actions" });
    await expect(trigger).toHaveCount(capabilityCase.edit || capabilityCase.remove || capabilityCase.download ? 1 : 0);
    if (capabilityCase.edit || capabilityCase.remove || capabilityCase.download) {
      await trigger.click();
      await expect(page.getByRole("menuitem", { name: "Edit details" })).toHaveCount(Number(capabilityCase.edit));
      await expect(page.getByRole("menuitem", { name: "Delete conversation" })).toHaveCount(Number(capabilityCase.remove));
      const downloadEntry = page.getByRole("menuitem", { name: "Download", exact: true });
      await expect(downloadEntry).toHaveCount(Number(capabilityCase.download));
      await expect(page.getByRole("menuitem", { name: "Download recording" })).toHaveCount(0);
      if (capabilityCase.download) {
        const downloads = await openDownloadSubmenu(page);
        await expect(page.getByRole("menu", { name: "Download" })).toBeVisible();
        const recording = downloads.getByRole("menuitem", { name: "Download recording" });
        await expect(recording).toBeDisabled();
        await expect(recording).toHaveAccessibleName("Download Recording");
        await expect(recording).toHaveAccessibleDescription("No recording is available to download.");
        await expect(downloads.getByText("No recording is available to download.")).toBeVisible();
        await expect(recording).toHaveAttribute("title", "No recording is available to download.");
        const transcriptDownload = downloads.getByRole("menuitem", { name: "Download transcript" });
        await expect(transcriptDownload).toBeFocused();
        await expect(transcriptDownload).toBeEnabled();
        await expect(downloads.getByRole("menuitem", { name: "Download summary" })).toBeEnabled();
        const notesDownload = downloads.getByRole("menuitem", { name: "Download notes" });
        await expect(notesDownload).toBeDisabled();
        await expect(notesDownload).toHaveAccessibleDescription("No notes are available to download.");
        await expect(downloads.getByText("No notes are available to download.")).toBeVisible();
        await page.keyboard.press("ArrowDown");
        await expect(downloads.getByRole("menuitem", { name: "Download summary" })).toBeFocused();
        await page.keyboard.press("ArrowLeft");
        await expect(downloadEntry).toBeFocused();
        await expect(downloads).toHaveCount(0);
        await page.keyboard.press("ArrowRight");
        await expect(page.getByRole("menuitem", { name: "Download transcript" })).toBeFocused();
        await page.keyboard.press("Escape");
        await expect(trigger).toBeFocused();
        await expect(page.getByRole("menu")).toHaveCount(0);
      }
    }
    await context.close();
  }
});

test("gear menu downloads the recording and complete JSON artifacts as named UTF-8 files", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000049";
  const conversation = { ...browserConversation(id, "2026-04-03T12:00:00Z", true), name: "Review: Bépo / Q4" };
  conversation.transcript_available = true;
  conversation.summary_available = true;
  const transcript = {
    language: "en" as const,
    segments: [{ evidence_id: "span-1", speaker: "Speaker 1", start_seconds: 0.125, end_seconds: 1.75, text: "Café, 東京, and full context." }],
    provider: "test-provider",
    model: "test-model",
  };
  const summary = {
    overview: { text: "Résumé with details ✓", evidence_span_ids: ["span-1"], confidence: "certain" as const, owner: "Alex", due_date: "2026-04-10" },
    key_topics: [{ text: "Planning", evidence_span_ids: ["span-1"], confidence: "uncertain" as const, owner: null, due_date: null }],
    decisions: [],
    action_items: [],
    provider: "summary-provider",
    model: "summary-model",
  };
  const exportedNotes = Array.from({ length: 55 }, (_, index) => ({
    id: `note-${index + 1}`,
    conversation_id: id,
    offset_seconds: index + 0.25,
    text: index === 54 ? "Café – note beyond the 50-item list limit ✓" : `Full note ${index + 1}`,
    subject: "local-development",
    author_kind: "human",
    delegated_session_id: null,
    created_at: `2026-04-03T12:00:${String(index % 60).padStart(2, "0")}Z`,
  }));
  let grantRequests = 0;
  const recordingDownloadRequests: Array<{ url: URL; resourceType: string }> = [];
  await installLocalAuth(page, ["context:read"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: exportedNotes.slice(0, 50) }));
  await page.route(`**/v1/conversations/${id}/notes/export`, (route) => route.fulfill({ json: exportedNotes }));
  await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({ json: transcript }));
  await page.route(`**/v1/conversations/${id}/summary`, (route) => route.fulfill({ json: summary }));
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => {
    grantRequests += 1;
    return route.fulfill({ json: { grant: grantRequests === 1 ? "playback-grant" : "fresh-download-grant" } });
  });
  await page.route((url) => url.pathname === `/v1/conversations/${id}/recording`, async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("download") === "true") {
      recordingDownloadRequests.push({ url, resourceType: route.request().resourceType() });
      await route.fulfill({
        contentType: "audio/wav",
        headers: { "Content-Disposition": 'attachment; filename="20260403-050000--review-bepo-q4.wav"' },
        body: waveFile(),
      });
      return;
    }
    await fulfillAudioStream(route, waveFile());
  });

  await page.goto(`/recordings/${id}`);
  const audio = page.getByTestId("recording-audio");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBe(10);
  const trigger = page.getByRole("button", { name: "Conversation actions" });
  await trigger.click();
  const downloads = await openDownloadSubmenu(page);
  await expect(downloads.getByRole("menuitem", { name: "Download recording" })).toBeEnabled();
  await expect(downloads.getByRole("menuitem", { name: "Download transcript" })).toBeEnabled();
  await expect(downloads.getByRole("menuitem", { name: "Download notes" })).toBeEnabled();
  await expect(downloads.getByRole("menuitem", { name: "Download summary" })).toBeEnabled();
  await expect(page.locator(".recording-player").getByRole("button", { name: "Export recording" })).toHaveCount(0);

  const recordingEvent = page.waitForEvent("download");
  await downloads.getByRole("menuitem", { name: "Download recording" }).click();
  const recordingDownload = await recordingEvent;
  expect(recordingDownload.suggestedFilename()).toBe("20260403-050000--review-bepo-q4.wav");
  expect((await readFile(await recordingDownload.path())).equals(waveFile())).toBe(true);
  await expect(downloads.getByRole("menuitem", { name: "Recording download started" })).toBeEnabled();
  expect(grantRequests).toBe(2);
  expect(recordingDownloadRequests).toHaveLength(1);
  expect(recordingDownloadRequests[0]?.url.pathname).toBe(`/v1/conversations/${id}/recording`);
  expect(recordingDownloadRequests[0]?.url.searchParams.get("grant")).toBe("fresh-download-grant");
  expect(recordingDownloadRequests[0]?.url.searchParams.get("download")).toBe("true");
  expect(recordingDownloadRequests[0]?.url.searchParams.has("mode")).toBe(false);
  expect(recordingDownloadRequests[0]?.resourceType).toBe("document");

  const transcriptEvent = page.waitForEvent("download");
  await downloads.getByRole("menuitem", { name: "Download transcript" }).click();
  const transcriptDownload = await transcriptEvent;
  expect(transcriptDownload.suggestedFilename()).toBe("20260403-050000--review-bepo-q4-transcript.json");
  expect(JSON.parse(await readFile(await transcriptDownload.path(), "utf8"))).toEqual(transcript);

  const notesEvent = page.waitForEvent("download");
  await downloads.getByRole("menuitem", { name: "Download notes" }).click();
  const notesDownload = await notesEvent;
  expect(notesDownload.suggestedFilename()).toBe("20260403-050000--review-bepo-q4-notes.json");
  expect(JSON.parse(await readFile(await notesDownload.path(), "utf8"))).toEqual(exportedNotes);
  expect(JSON.parse(await readFile(await notesDownload.path(), "utf8"))).toHaveLength(55);

  const summaryEvent = page.waitForEvent("download");
  await downloads.getByRole("menuitem", { name: "Download summary" }).click();
  const summaryDownload = await summaryEvent;
  expect(summaryDownload.suggestedFilename()).toBe("20260403-050000--review-bepo-q4-summary.json");
  expect(JSON.parse(await readFile(await summaryDownload.path(), "utf8"))).toEqual(summary);
  await expect(page.locator(".recording-player")).toHaveAttribute("data-playback-source", "stream");
});

test("gear downloads report failures without hiding the menu or changing playback", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000050";
  const conversation = { ...browserConversation(id, "2026-04-03T12:00:00Z", true), transcript_available: true, summary_available: true };
  let grantRequests = 0;
  await installLocalAuth(page, ["context:read"]);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [{
    id: "note-1", conversation_id: id, offset_seconds: 1, text: "Available", subject: "local-development", author_kind: "human", created_at: "2026-04-03T12:00:01Z",
  }] }));
  await page.route(`**/v1/conversations/${id}/recording-access`, (route) => {
    grantRequests += 1;
    return route.fulfill(grantRequests === 1
      ? { json: { grant: "playback-grant" } }
      : { status: 503, json: { error: { kind: "service_unavailable", message: "Unavailable" } } });
  });
  await page.route(`**/v1/conversations/${id}/recording?grant=**`, (route) => fulfillAudioStream(route, waveFile()));
  await page.route(`**/v1/conversations/${id}/speakers`, (route) => route.fulfill({ json: { speaker_revision: 1, speakers: [], summary_job: null } }));
  await page.route(`**/v1/conversations/${id}/transcript`, (route) => route.fulfill({
    status: 503,
    json: { error: { kind: "service_unavailable", message: "Unavailable" } },
  }));
  await page.route(`**/v1/conversations/${id}/notes/export`, (route) => route.fulfill({
    status: 503,
    json: { error: { kind: "service_unavailable", message: "Unavailable" } },
  }));
  await page.route(`**/v1/conversations/${id}/summary`, (route) => route.fulfill({
    status: 503,
    json: { error: { kind: "service_unavailable", message: "Unavailable" } },
  }));

  await page.goto(`/recordings/${id}`);
  await expect(page.getByTestId("recording-audio")).toHaveAttribute("src", /recording\?grant=playback-grant/);
  await page.getByRole("button", { name: "Conversation actions" }).click();
  const menu = page.getByRole("menu", { name: "Conversation actions" });
  const downloads = await openDownloadSubmenu(page);
  for (const [label, expectedError] of [
    ["recording", "Recording could not be downloaded."],
    ["transcript", "Transcript could not be downloaded."],
    ["notes", "Notes could not be downloaded."],
    ["summary", "Summary could not be downloaded."],
  ]) {
    await downloads.getByRole("menuitem", { name: `Download ${label}` }).click();
    await expect(page.locator(".conversation-action-error")).toHaveText(expectedError);
    await expect(menu).toBeVisible();
    await expect(page.getByTestId("recording-audio")).toHaveAttribute("src", /recording\?grant=playback-grant/);
  }
});

test("delete confirmation can be canceled and deletion errors remain visible outside Status", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000045";
  const conversation = browserConversation(id, "2026-04-01T12:00:00Z");
  let deleteRequests = 0;
  await installLocalAuth(page);
  await page.route("**/v1/conversations", (route) => route.fulfill({ json: [conversation] }));
  await page.route(`**/v1/conversations/${id}/jobs`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/v1/conversations/${id}`, async (route) => {
    if (route.request().method() !== "DELETE") {
      await route.continue();
      return;
    }
    deleteRequests += 1;
    await route.fulfill({ status: 503, json: { error: { kind: "service_unavailable", message: "Unavailable" } } });
  });
  await page.goto(`/recordings/${id}`);
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "Conversation actions" }).click();
  await page.getByRole("menuitem", { name: "Delete conversation" }).click();
  await expect(page.getByRole("heading", { name: conversation.name })).toBeVisible();
  expect(deleteRequests).toBe(0);

  await page.getByRole("tab", { name: "Notes" }).click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Conversation actions" }).click();
  await page.getByRole("menuitem", { name: "Delete conversation" }).click();
  const error = page.locator(".conversation-action-error");
  await expect(error).toHaveText("Deletion failed.");
  await expect(error).toBeVisible();
  await expect(page.locator(".review-panel .conversation-action-error")).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Notes" })).toHaveAttribute("aria-selected", "true");
  expect(deleteRequests).toBe(1);
});

test("conversation deletion sends the required idempotency key", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000001";
  const conversation = {
    id,
    name: "Test recording",
    description: null,
    occurred_at: "2026-01-01T00:00:00Z",
    people: [],
    tags: [],
    revision: 1,
    sync_state: "ready",
    recording_available: false,
    transcript_available: false,
    summary_available: false,
  };
  let deleted = false;
  let deleteKey: string | undefined;

  page.on("dialog", (dialog) => dialog.accept());
  await page.route("**/v1/conversations", async (route) => {
    await route.fulfill({ json: deleted ? [] : [conversation] });
  });
  await page.route(`**/v1/conversations/${id}`, async (route) => {
    if (route.request().method() !== "DELETE") {
      await route.continue();
      return;
    }
    deleteKey = route.request().headers()["x-idempotency-key"];
    deleted = true;
    await route.fulfill({ status: 202, json: { kind: "conversation_deleted", conversation_id: id } });
  });
  await page.route(`**/v1/conversations/${id}/notes`, (route) => route.fulfill({ json: [] }));

  await page.goto("/");
  await page.getByRole("button", { name: /Test recording/ }).click();
  await page.getByRole("button", { name: "Conversation actions" }).click();
  await page.getByRole("menuitem", { name: "Delete conversation" }).click();
  await expect(page.getByRole("button", { name: /Test recording/ })).toHaveCount(0);
  expect(deleteKey).toBe(id);
});

test("microphone denial is visible", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: () => Promise.reject(new DOMException("denied", "NotAllowedError")) },
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Start recording" }).click();
  await expect(page.getByRole("alert")).toHaveText("Microphone permission is required to record.");
});

test("transcript rows seek and start or resume playback without rename side effects", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000091";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z", true), transcript_available: true };
  const transcript: Transcript = {
    language: "en",
    segments: [
      { evidence_id: "segment-one", speaker: "Speaker 1", start_seconds: 1, end_seconds: 3, text: "First timed segment." },
      { evidence_id: "segment-two", speaker: "Speaker 2", start_seconds: 5, end_seconds: 7, text: "Second timed segment." },
    ],
    provider: "test",
    model: "test",
  };
  const mapping: SpeakerMapping = {
    speaker_revision: 1,
    speakers: [
      { speaker_number: 1, label: "Speaker 1", display_name: null },
      { speaker_number: 2, label: "Speaker 2", display_name: null },
    ],
    summary_job: null,
  };
  await openTranscriptReview(page, conversation, transcript, mapping);
  const audio = page.getByTestId("recording-audio");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBe(10);
  await audio.evaluate((element) => {
    const player = element as HTMLAudioElement;
    player.playbackRate = 0.1;
    player.dataset.playEvents = "0";
    player.addEventListener("play", () => { player.dataset.playEvents = String(Number(player.dataset.playEvents) + 1); });
  });
  const firstRow = page.locator(".transcript-segment").nth(0);
  const secondRow = page.locator(".transcript-segment").nth(1);
  await firstRow.locator("p").click();
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(0.95);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();

  await page.getByRole("button", { name: "Pause recording" }).click();
  await secondRow.locator("p").click();
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(4.95);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
  await firstRow.locator("p").click();
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(0.95);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();

  await page.getByRole("button", { name: "Pause recording" }).click();
  const seekAction = page.getByRole("button", { name: "Play segment from 00:05.00" });
  await seekAction.focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(4.95);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();

  await page.getByRole("button", { name: "Pause recording" }).click();
  await audio.evaluate((element) => {
    const player = element as HTMLAudioElement;
    player.currentTime = 8.25;
    player.dispatchEvent(new Event("timeupdate"));
  });
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeCloseTo(8.25, 1);
  const playEventsBeforeEdit = await audio.getAttribute("data-play-events");
  const speakerEdit = page.getByRole("button", { name: "Edit speaker name for Speaker 1" });
  await speakerEdit.click();
  const nameInput = page.getByRole("textbox", { name: "Name for Speaker 1" });
  await nameInput.click();
  await nameInput.fill("A renamed speaker");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeCloseTo(8.25, 1);
  await page.getByRole("button", { name: "Save name" }).click();
  await expect(page.getByRole("button", { name: "Edit speaker name for Speaker 1" })).toHaveText("A renamed speaker");
  await expect(page.getByRole("button", { name: "Play recording" })).toBeVisible();
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeCloseTo(8.25, 1);
  await expect(audio).toHaveAttribute("data-play-events", playEventsBeforeEdit ?? "0");

  await page.getByRole("button", { name: "Edit speaker name for Speaker 1" }).click();
  await page.getByRole("textbox", { name: "Name for Speaker 1" }).fill("Discard this name");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Edit speaker name for Speaker 1" })).toHaveText("A renamed speaker");
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeCloseTo(8.25, 1);
  await expect(audio).toHaveAttribute("data-play-events", playEventsBeforeEdit ?? "0");
});

test("transcript row requests wait for audio metadata before seeking and playing", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000092";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z", true), transcript_available: true };
  const transcript: Transcript = {
    language: "en",
    segments: [{ evidence_id: "delayed-segment", speaker: "Speaker 1", start_seconds: 4, end_seconds: 6, text: "This segment loads later." }],
    provider: "test",
    model: "test",
  };
  const mapping: SpeakerMapping = {
    speaker_revision: 1,
    speakers: [{ speaker_number: 1, label: "Speaker 1", display_name: null }],
    summary_job: null,
  };
  let signalAudioRequest: () => void = () => undefined;
  let releaseAudioResponse: () => void = () => undefined;
  const audioRequested = new Promise<void>((resolve) => { signalAudioRequest = resolve; });
  const audioResponseGate = new Promise<void>((resolve) => { releaseAudioResponse = resolve; });
  await openTranscriptReview(page, conversation, transcript, mapping, async (route) => {
    signalAudioRequest();
    await audioResponseGate;
    await fulfillAudioStream(route, waveFile());
  });
  const audio = page.getByTestId("recording-audio");
  await expect(audio).toHaveAttribute("src", /transcript-test-grant/);
  await audioRequested;
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).readyState)).toBeLessThan(1);
  await expect(page.getByRole("button", { name: "Play segment from 00:04.00" })).toBeEnabled();
  await page.locator(".transcript-segment p").click();
  releaseAudioResponse();
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).duration)).toBe(10);
  await expect.poll(() => audio.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThanOrEqual(3.95);
  await expect(page.getByRole("button", { name: "Pause recording" })).toBeVisible();
});

test("transcript speaker and text columns stay aligned with long names on desktop and mobile", async ({ browser }) => {
  const id = "00000000-0000-0000-0000-000000000093";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z", true), transcript_available: true };
  const transcript: Transcript = {
    language: "en",
    segments: [
      { evidence_id: "long-speaker", speaker: "Speaker 1", start_seconds: 0, end_seconds: 1, text: "A line spoken by a speaker with a very long display name." },
      { evidence_id: "short-speaker", speaker: "Speaker 2", start_seconds: 1, end_seconds: 2, text: "A different speaker." },
    ],
    provider: "test",
    model: "test",
  };
  const mapping: SpeakerMapping = {
    speaker_revision: 1,
    speakers: [
      { speaker_number: 1, label: "Speaker 1", display_name: "Professor Alexandria-Benedictine-Sandhurst-Montgomery-Worthington the Third with a very long name" },
      { speaker_number: 2, label: "Speaker 2", display_name: "Kai" },
    ],
    summary_job: null,
  };
  for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
    const context = await browser.newContext({ viewport });
    const page = await context.newPage();
    await openTranscriptReview(page, conversation, transcript, mapping);
    const rows = page.locator(".transcript-segment");
    await expect(rows).toHaveCount(2);
    const textColumnPositions = await rows.locator("p").evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().left));
    expect(Math.abs(textColumnPositions[0]! - textColumnPositions[1]!)).toBeLessThanOrEqual(1);
    const nameColumnPositions = await rows.locator(":scope > :nth-child(2)").evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().left));
    expect(Math.abs(nameColumnPositions[0]! - nameColumnPositions[1]!)).toBeLessThanOrEqual(1);

    const edit = page.getByRole("button", { name: "Edit speaker name for Speaker 2" });
    await edit.click();
    const input = page.getByRole("textbox", { name: "Name for Speaker 2" });
    await input.fill("The extraordinarily lengthy replacement speaker name that must wrap within its column");
    const editorTextPosition = await rows.nth(1).locator("p").evaluate((element) => element.getBoundingClientRect().left);
    expect(Math.abs(textColumnPositions[0]! - editorTextPosition)).toBeLessThanOrEqual(1);
    await page.getByRole("button", { name: "Save name" }).click();
    await expect(rows.nth(1).locator("p")).toHaveCSS("overflow-wrap", "anywhere");
    const renamedTextPositions = await rows.locator("p").evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().left));
    expect(Math.abs(renamedTextPositions[0]! - renamedTextPositions[1]!)).toBeLessThanOrEqual(1);
    const dimensions = await page.evaluate(() => ({ documentWidth: document.documentElement.scrollWidth, viewportWidth: window.innerWidth }));
    expect(dimensions.documentWidth).toBeLessThanOrEqual(dimensions.viewportWidth);
    await context.close();
  }
});

test("transcript rows do not request playback when audio is unavailable", async ({ page }) => {
  const id = "00000000-0000-0000-0000-000000000094";
  const conversation = { ...browserConversation(id, "2026-04-04T12:00:00Z"), transcript_available: true };
  const transcript: Transcript = {
    language: "en",
    segments: [{ evidence_id: "unavailable-segment", speaker: "Speaker 1", start_seconds: 2, end_seconds: 4, text: "This recording is unavailable." }],
    provider: "test",
    model: "test",
  };
  const mapping: SpeakerMapping = {
    speaker_revision: 1,
    speakers: [{ speaker_number: 1, label: "Speaker 1", display_name: null }],
    summary_job: null,
  };
  await openTranscriptReview(page, conversation, transcript, mapping);
  await expect(page.locator(".player-unavailable")).toHaveText("No recording is available for playback.");
  const seekAction = page.getByRole("button", { name: "Play segment from 00:02.00" });
  const unavailableMessage = "Transcript playback is unavailable because no recording is available.";
  await expect(page.getByRole("status").filter({ hasText: unavailableMessage })).toBeVisible();
  await expect(seekAction).toBeDisabled();
  await expect(seekAction).toHaveAccessibleDescription(unavailableMessage);
  await page.locator(".transcript-segment p").click();
  await expect(page.getByTestId("recording-audio")).toHaveCount(0);
  await expect(page.locator(".player-unavailable")).toHaveText("No recording is available for playback.");
});
