import { useCallback, useEffect, useRef, useState } from "react";

export type CaptureState = "idle" | "recording" | "ready" | "uploading" | "failed";

export interface PendingRecording {
  blob: Blob;
  conversationId: string;
  occurredAt: string;
}

export function noteOffsetSeconds(currentTime: number): number {
  return Math.round(Math.max(0, currentTime) * 1000) / 1000;
}

export function useAudioCapture() {
  const recorder = useRef<MediaRecorder | null>(null);
  const audioContext = useRef<AudioContext | null>(null);
  const chunks = useRef<Blob[]>([]);
  const [state, setState] = useState<CaptureState>("idle");
  const [pending, setPending] = useState<PendingRecording | null>(null);
  const [analyser, setAnalyser] = useState<AnalyserNode | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const startedAt = useRef<number | null>(null);

  const discard = useCallback(() => {
    chunks.current = [];
    setPending(null);
    setError(null);
    startedAt.current = null;
    setElapsedSeconds(0);
    setState("idle");
  }, []);

  useEffect(() => {
    const release = () => {
      recorder.current?.stream.getTracks().forEach((track) => track.stop());
      recorder.current = null;
      chunks.current = [];
      setPending(null);
      setAnalyser(null);
      const context = audioContext.current;
      audioContext.current = null;
      if (context && context.state !== "closed") void context.close();
      startedAt.current = null;
      setElapsedSeconds(0);
    };
    window.addEventListener("pagehide", release);
    return () => {
      window.removeEventListener("pagehide", release);
      release();
    };
  }, []);

  async function start() {
    setError(null);
    let stream: MediaStream | null = null;
    let context: AudioContext | null = null;
    try {
      try {
        context = new AudioContext();
        if (context.state === "suspended") void context.resume().catch(() => undefined);
      } catch {
        context = null;
      }
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const nextRecorder = new MediaRecorder(stream);
      let nextAnalyser: AnalyserNode | null = null;
      if (context) {
        try {
          nextAnalyser = context.createAnalyser();
          nextAnalyser.fftSize = 1024;
          const source = context.createMediaStreamSource(stream);
          const silentOutput = context.createGain();
          silentOutput.gain.value = 0;
          source.connect(nextAnalyser);
          nextAnalyser.connect(silentOutput);
          silentOutput.connect(context.destination);
          audioContext.current = context;
          setAnalyser(nextAnalyser);
        } catch {
          if (context.state !== "closed") void context.close();
          context = null;
        }
      }
      chunks.current = [];
      nextRecorder.addEventListener("dataavailable", (event) => {
        if (event.data.size > 0) chunks.current.push(event.data);
      });
      nextRecorder.addEventListener("stop", () => {
        const blob = new Blob(chunks.current, { type: nextRecorder.mimeType || "audio/webm" });
        chunks.current = [];
        nextRecorder.stream.getTracks().forEach((track) => track.stop());
        recorder.current = null;
        setPending({ blob, conversationId: crypto.randomUUID(), occurredAt: new Date().toISOString() });
        setAnalyser(null);
        const currentContext = audioContext.current;
        audioContext.current = null;
        if (currentContext && currentContext.state !== "closed") void currentContext.close();
        setState("ready");
      }, { once: true });
      recorder.current = nextRecorder;
      startedAt.current = Date.now();
      setElapsedSeconds(0);
      nextRecorder.start();
      setState("recording");
    } catch {
      stream?.getTracks().forEach((track) => track.stop());
      recorder.current = null;
      const currentContext = audioContext.current ?? context;
      audioContext.current = null;
      if (currentContext && currentContext.state !== "closed") void currentContext.close();
      setAnalyser(null);
      setError("Microphone permission is required to record.");
    }
  }

  function stop() {
    const active = recorder.current;
    if (!active || active.state === "inactive") return;
    active.stop();
  }

  useEffect(() => {
    if (state !== "recording") return;
    const timer = window.setInterval(() => {
      if (startedAt.current) setElapsedSeconds(Math.floor((Date.now() - startedAt.current) / 1000));
    }, 250);
    return () => window.clearInterval(timer);
  }, [state]);

  return { state, setState, pending, analyser, error, setError, elapsedSeconds, start, stop, discard };
}
