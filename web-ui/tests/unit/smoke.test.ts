import { describe, expect, it } from "vitest";

import { recordingDownloadFilename, recordingMediaType } from "../../src/api";
import { noteOffsetSeconds } from "../../src/capture";

describe("web unit runner", () => {
  it("does not collect Playwright specs", () => {
    expect(true).toBe(true);
  });
});

describe("recording note offsets", () => {
  it("rounds to the nearest millisecond and clamps negative offsets", () => {
    expect(noteOffsetSeconds(6.9274)).toBe(6.927);
    expect(noteOffsetSeconds(6.9276)).toBe(6.928);
    expect(noteOffsetSeconds(-0.5)).toBe(0);
  });
});

describe("recording download filenames", () => {
  it("combines a Pacific timestamp with a parameterized title", () => {
    expect(recordingDownloadFilename("2026-10-14T23:32:00Z", "Therapy", "mp3")).toBe("20261014-163200--therapy.mp3");
  });
});

describe("recording media types", () => {
  it.each([
    ["voice.wav", "", "audio/wav"],
    ["voice.mp4", "video/mp4", "audio/mp4"],
    ["voice.aac", "", "audio/aac"],
    ["voice.webm", "audio/webm;codecs=opus", "audio/webm"],
    ["voice.ogg", "", "audio/ogg"],
    ["voice.mp3", "", "audio/mpeg"],
    ["voice.wav", "audio/x-wav", "audio/x-wav"],
  ])("recognizes %s", (fileName, contentType, expected) => {
    expect(recordingMediaType(fileName, contentType)).toBe(expected);
  });

  it("rejects unsupported formats", () => {
    expect(recordingMediaType("notes.txt", "text/plain")).toBeNull();
  });
});
