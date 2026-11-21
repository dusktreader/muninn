export type Capability =
  | "context:read"
  | "metadata:write"
  | "processing:admin"
  | "deletion:write"
  | "notes:human:create"
  | "notes:human:edit"
  | "notes:human:delete";

export interface AuthSession {
  accessToken?: string;
  displayName: string;
  capabilities: ReadonlySet<Capability>;
  onLogout?: () => void;
  onSessionExpired?: () => void;
}

export interface Conversation {
  id: string;
  name: string;
  description: string | null;
  occurred_at: string;
  people: string[];
  tags: string[];
  revision: number;
  sync_state: string;
  recording_available: boolean;
  transcript_available: boolean;
  summary_available: boolean;
}

export interface TranscriptSegment {
  evidence_id: string;
  speaker: string;
  start_seconds: number;
  end_seconds: number;
  text: string;
}

export interface Transcript {
  language: "en";
  segments: TranscriptSegment[];
  provider: string;
  model: string;
}

export interface SummaryItem {
  text: string;
  evidence_span_ids: string[];
  confidence: "certain" | "uncertain";
  owner: string | null;
  due_date: string | null;
}

export interface Summary {
  overview: SummaryItem | null;
  key_topics: SummaryItem[];
  decisions: SummaryItem[];
  action_items: SummaryItem[];
  provider: string;
  model: string;
}

export type ProcessingJobState = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type ProcessingJobKind = "transcription" | "summarization";

export interface ProcessingJob {
  id: string;
  kind: ProcessingJobKind;
  state: ProcessingJobState;
  attempts: number;
  created_at: string;
  last_error_code: string | null;
  last_error_details: string | null;
}

export interface ConversationSpeaker {
  speaker_number: number;
  label: string;
  display_name: string | null;
}

export interface SpeakerMapping {
  speaker_revision: number;
  speakers: ConversationSpeaker[];
  summary_job: ProcessingJob | null;
}

export interface Note {
  id: string;
  conversation_id: string;
  offset_seconds: number;
  text: string;
  author_kind: "human" | "agent";
  subject: string;
  created_at: string;
}

interface ErrorEnvelope {
  error?: { kind?: string; message?: string };
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly kind: string,
  ) {
    super(message);
  }
}

export class OfflineError extends Error {}

export type AudioExportFormat = "aac" | "mp3" | "mp4" | "ogg" | "wav" | "webm";

const mediaTypesByExtension: Record<string, string> = {
  aac: "audio/aac",
  mp3: "audio/mpeg",
  m4a: "audio/mp4",
  mp4: "audio/mp4",
  ogg: "audio/ogg",
  wav: "audio/wav",
  webm: "audio/webm",
};

const supportedMediaTypes = new Set([
  "audio/aac",
  "audio/mpeg",
  "audio/mp4",
  "audio/ogg",
  "audio/wav",
  "audio/webm",
  "audio/x-wav",
]);

export function recordingMediaType(fileName: string, contentType: string): string | null {
  const declaredType = contentType.split(";")[0]?.trim().toLowerCase() ?? "";
  if (supportedMediaTypes.has(declaredType)) return declaredType;
  const extension = fileName.split(".").pop()?.toLowerCase() ?? "";
  return mediaTypesByExtension[extension] ?? null;
}

function conversationFilePrefix(occurredAt: string, title: string): string {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Los_Angeles",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(occurredAt)).map(({ type, value }) => [type, value]));
  const timestamp = `${parts.year}${parts.month}${parts.day}-${parts.hour}${parts.minute}${parts.second}`;
  const slug = title.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "recording";
  return `${timestamp}--${slug}`;
}

export function recordingDownloadFilename(occurredAt: string, title: string, extension: string): string {
  return `${conversationFilePrefix(occurredAt, title)}.${extension}`;
}

export function jsonDownloadFilename(occurredAt: string, title: string, artifact: "transcript" | "notes" | "summary"): string {
  return `${conversationFilePrefix(occurredAt, title)}-${artifact}.json`;
}

function mediaTypeExtension(mediaType: string): string {
  return {
    "audio/aac": "aac",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
  }[mediaType] ?? "audio";
}

export class ApiClient {
  constructor(
    private readonly session: AuthSession,
    private readonly baseUrl = import.meta.env.VITE_MUNINN_API_URL ?? "",
  ) {}

  private async binaryRequest(url: string, signal?: AbortSignal): Promise<Blob> {
    const headers = new Headers();
    if (this.session.accessToken) headers.set("Authorization", `Bearer ${this.session.accessToken}`);
    const response = await fetch(url, { headers, signal });
    if (!response.ok) {
      if (response.status === 401) this.session.onSessionExpired?.();
      const body = (await response.json().catch(() => ({}))) as ErrorEnvelope;
      throw new ApiError(body.error?.message ?? "The recording could not be loaded.", response.status, body.error?.kind ?? "request_failed");
    }
    return response.blob();
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (this.session.accessToken) headers.set("Authorization", `Bearer ${this.session.accessToken}`);
    if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
    const response = await fetch(`${this.baseUrl}${path}`, { ...init, headers });
    if (!response.ok) {
      if (response.status === 401) this.session.onSessionExpired?.();
      const body = (await response.json().catch(() => ({}))) as ErrorEnvelope;
      throw new ApiError(body.error?.message ?? "The server rejected the request.", response.status, body.error?.kind ?? "request_failed");
    }
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  listConversations(signal?: AbortSignal): Promise<Conversation[]> {
    return this.request("/v1/conversations", { signal });
  }

  getConversation(id: string): Promise<Conversation> {
    return this.request(`/v1/conversations/${id}`);
  }

  getTranscript(id: string): Promise<Transcript> {
    return this.request(`/v1/conversations/${id}/transcript`);
  }

  getSpeakers(id: string): Promise<SpeakerMapping> {
    return this.request(`/v1/conversations/${id}/speakers`);
  }

  renameSpeaker(id: string, expectedSpeakerRevision: number, speakerNumber: number, displayName: string | null): Promise<SpeakerMapping> {
    return this.request(`/v1/conversations/${id}/speakers`, {
      method: "PATCH",
      headers: { "X-Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        expected_speaker_revision: expectedSpeakerRevision,
        speaker_number: speakerNumber,
        display_name: displayName,
      }),
    });
  }

  getSummary(id: string): Promise<Summary> {
    return this.request(`/v1/conversations/${id}/summary`);
  }

  listJobs(id: string): Promise<ProcessingJob[]> {
    return this.request(`/v1/conversations/${id}/jobs`);
  }

  stageTranscription(id: string): Promise<ProcessingJob> {
    return this.request(`/v1/conversations/${id}/jobs/transcription`, {
      method: "POST",
      headers: { "X-Idempotency-Key": crypto.randomUUID() },
    });
  }

  stageSummarization(id: string): Promise<ProcessingJob> {
    return this.request(`/v1/conversations/${id}/jobs/summarization`, {
      method: "POST",
      headers: { "X-Idempotency-Key": crypto.randomUUID() },
    });
  }

  recordingUrl(id: string, download = false, format?: AudioExportFormat, grant?: string, mode: "stream" | "full" = "stream"): string {
    const url = `${this.baseUrl}/v1/conversations/${id}/recording`;
    const query = new URLSearchParams();
    if (download) query.set("download", "true");
    if (format) query.set("format", format);
    if (grant) query.set("grant", grant);
    if (mode === "full") query.set("mode", "full");
    return query.size ? `${url}?${query.toString()}` : url;
  }

  async recordingAccessGrant(id: string): Promise<string> {
    const result = await this.request<{ grant: string }>(`/v1/conversations/${id}/recording-access`, {
      method: "POST",
    });
    return result.grant;
  }

  async downloadRecording(id: string, grant: string, signal?: AbortSignal): Promise<Blob> {
    return this.binaryRequest(this.recordingUrl(id, true, undefined, grant, "full"), signal);
  }

  createConversation(id: string, occurredAt: string, name = "Browser recording"): Promise<Conversation> {
    return this.request("/v1/conversations", {
      method: "POST",
      headers: { "X-Idempotency-Key": id },
      body: JSON.stringify({ id, name, occurred_at: occurredAt, people: [], tags: [] }),
    });
  }

  async uploadRecording(conversationId: string, recording: Blob, fileName?: string): Promise<void> {
    if (!navigator.onLine) throw new OfflineError("You are offline. The recording is still held in this tab.");
    const mediaType = recordingMediaType(fileName ?? "recording", recording.type);
    if (!mediaType) throw new Error("Choose a supported audio format.");
    const upload = recording.type === mediaType ? recording : new Blob([recording], { type: mediaType });
    const form = new FormData();
    form.append("recording", upload, fileName ?? `recording.${mediaTypeExtension(mediaType)}`);
    const digest = await crypto.subtle.digest("SHA-256", await recording.arrayBuffer());
    const digestHex = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    await this.request(`/v1/conversations/${conversationId}/recording`, {
      method: "POST",
      headers: { "X-Idempotency-Key": conversationId, "X-Content-SHA256": digestHex },
      body: form,
    });
  }

  updateConversation(conversation: Conversation, changes: Pick<Conversation, "name" | "description" | "people" | "tags">): Promise<Conversation> {
    return this.request(`/v1/conversations/${conversation.id}`, {
      method: "PATCH",
      headers: { "X-Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ ...changes, expected_revision: conversation.revision }),
    });
  }

  deleteConversation(id: string): Promise<{ code: string }> {
    return this.request(`/v1/conversations/${id}`, {
      method: "DELETE",
      headers: { "X-Idempotency-Key": id },
    });
  }

  listNotes(id: string): Promise<Note[]> {
    return this.request(`/v1/conversations/${id}/notes`);
  }

  exportNotes(id: string): Promise<Note[]> {
    return this.request(`/v1/conversations/${id}/notes/export`);
  }

  createNote(id: string, offsetSeconds: number, text: string): Promise<Note> {
    return this.request(`/v1/conversations/${id}/notes`, {
      method: "POST",
      headers: { "X-Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ offset_seconds: offsetSeconds, text }),
      signal: AbortSignal.timeout(15_000),
    });
  }

  updateNote(conversationId: string, noteId: string, text: string): Promise<Note> {
    return this.request(`/v1/conversations/${conversationId}/notes/${noteId}`, {
      method: "PATCH",
      headers: { "X-Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ text }),
      signal: AbortSignal.timeout(15_000),
    });
  }

  deleteNote(conversationId: string, noteId: string): Promise<void> {
    return this.request(`/v1/conversations/${conversationId}/notes/${noteId}`, { method: "DELETE" });
  }
}
