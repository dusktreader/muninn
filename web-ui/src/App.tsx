import { FormEvent, KeyboardEvent as ReactKeyboardEvent, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, matchPath, Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router";

import { ApiClient, ApiError, AuthSession, Conversation, ConversationSpeaker, Note, OfflineError, ProcessingJob, SpeakerMapping, Summary, Transcript, jsonDownloadFilename, recordingMediaType } from "./api";
import { noteOffsetSeconds, PendingRecording, useAudioCapture } from "./capture";

type UploadStatus = "creating" | "uploading" | "uploaded" | "failed";
type MetadataDraft = Pick<Conversation, "name" | "description" | "people" | "tags">;
type ImportJob = { file: File; conversationId: string; occurredAt: string; status: UploadStatus; error: string | null };
type RecordingSortKey = "date" | "name";
type ReviewTab = "status" | "notes" | "transcript" | "summary";
type PlaybackSource = { kind: "stream" | "local"; url: string };
type PlaybackState = "idle" | "loading" | "playing" | "paused" | "buffering" | "ended" | "failed";
type SourcePhase = "resolving" | "ready" | "failed";
type DownloadArtifact = "recording" | "transcript" | "notes" | "summary";
type ArtifactDownloadState = "idle" | "downloading" | "initiated" | "downloaded" | "failed";

const supportedAudioAccept = ".aac,.m4a,.mp3,.mp4,.ogg,.wav,.webm,audio/aac,audio/mpeg,audio/mp4,audio/ogg,audio/wav,audio/x-wav,audio/webm";

function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function startNativeDownload(url: string): void {
  const link = document.createElement("a");
  link.href = url;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
}

function messageFor(error: unknown): string {
  if (error instanceof OfflineError) return error.message;
  if (error instanceof ApiError && error.status === 409) return "This conversation changed elsewhere. Refresh before editing again.";
  if (error instanceof ApiError && error.status === 401) return "Your session expired. Sign in again before retrying.";
  if (error instanceof ApiError && error.status === 403) return "Your account does not have permission for this action.";
  return "Upload failed. The recording is still held in this tab and can be retried.";
}

function formatTime(seconds: number): string {
  const milliseconds = Number.isFinite(seconds) ? Math.max(0, Math.round(seconds * 1000)) : 0;
  const minutes = Math.floor(milliseconds / 60_000);
  const remainingSeconds = Math.floor((milliseconds % 60_000) / 1000);
  return `${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}.${String(Math.floor((milliseconds % 1000) / 10)).padStart(2, "0")}`;
}

function noteMarkerTitle(text: string): string {
  const characters = Array.from(text);
  return characters.length <= 128 ? text : `${characters.slice(0, 127).join("")}…`;
}

function jobStateLabel(state: ProcessingJob["state"]): string {
  return {
    queued: "STAGED",
    running: "RUNNING",
    succeeded: "COMPLETED",
    failed: "FAILED",
    cancelled: "CANCELLED",
  }[state];
}

function initials(name: string): string {
  return name.trim().split(/\s+/).slice(0, 2).map((part) => part[0] ?? "").join("").toUpperCase();
}

function recordingTitle(fileName: string): string {
  const lastDot = fileName.lastIndexOf(".");
  const title = (lastDot > 0 ? fileName.slice(0, lastDot) : fileName).trim();
  return title || "Imported recording";
}

function StatusDisclosure({ conversation, uploadStatus, transcriptJob, summaryJob, jobsLoading = false, jobsError = null, defaultOpen = false }: {
  conversation: Conversation | null;
  uploadStatus: UploadStatus | null;
  transcriptJob?: ProcessingJob | null;
  summaryJob?: ProcessingJob | null;
  jobsLoading?: boolean;
  jobsError?: string | null;
  defaultOpen?: boolean;
}) {
  const uploadLabel = uploadStatus === "creating"
    ? "Creating"
    : uploadStatus === "uploading"
      ? "Uploading"
      : uploadStatus === "failed"
        ? "Upload failed"
        : conversation?.recording_available ? "Uploaded" : "Not uploaded";
  const lifecycle = conversation?.sync_state.replaceAll("_", " ") ?? "Not created";
  const transcript = conversation?.transcript_available
    ? "COMPLETED"
    : transcriptJob?.state === "running"
      ? "RUNNING"
      : transcriptJob?.state === "queued"
        ? "STAGED"
        : transcriptJob?.state === "failed" || conversation?.sync_state === "failed"
          ? "FAILED"
          : transcriptJob?.state === "succeeded"
            ? "COMPLETED"
            : jobsError
              ? "UNAVAILABLE"
              : jobsLoading
                ? "CHECKING"
                : conversation?.recording_available ? "READY" : "NOT STARTED";
  const summary = summaryJob?.state === "running"
    || summaryJob?.state === "queued"
    || summaryJob?.state === "failed"
    || summaryJob?.state === "cancelled"
    ? jobStateLabel(summaryJob.state)
    : conversation?.summary_available
      ? "COMPLETED"
      : summaryJob ? jobStateLabel(summaryJob.state) : jobsError
        ? "UNAVAILABLE"
        : jobsLoading
          ? "CHECKING"
          : conversation?.transcript_available ? "READY" : "NOT STARTED";
  const statusClass = uploadStatus ?? (conversation?.recording_available ? "uploaded" : "idle");

  return <details className="status-disclosure" open={defaultOpen || undefined}>
    <summary>
      <span className="status-disclosure-label">Status</span>
      <span className={`upload-badge ${statusClass}`} role="status">{uploadLabel}</span>
      <span className="transcript-state">Transcript {transcript}</span>
      <span className="status-disclosure-lifecycle">{lifecycle}</span>
    </summary>
    <div className="detail-status-grid">
      <div><span>Upload</span><strong>{uploadLabel}</strong></div>
      <div><span>Lifecycle</span><strong>{lifecycle}</strong></div>
      <div><span>Transcript</span><strong>{transcript}</strong></div>
      <div><span>Summary</span><strong>{summary}</strong></div>
    </div>
  </details>;
}

function StatusRow({ label, status, issue, progressLabel }: {
  label: string;
  status: string;
  issue?: string | null;
  progressLabel?: string;
}) {
  return <article className="status-row">
    <div className="status-row-heading"><strong>{label}</strong><span aria-live="polite" className={`status-value status-${status.toLowerCase().replaceAll(" ", "-")}`}>{status}</span></div>
    {progressLabel && (status === "STAGED" || status === "RUNNING") && <progress aria-label={progressLabel} aria-valuetext={status} />}
    {issue && <p role="alert" className="status-row-issue">{issue}</p>}
  </article>;
}

function JobProgress({ job }: { job: ProcessingJob }) {
  const description = job.kind === "transcription" ? "Transcribing the recording." : "Summarizing the transcript.";
  const progressLabel = job.kind === "transcription" ? "Transcription progress" : "Summary progress";
  const status = jobStateLabel(job.state);
  return <div className="transcript-progress" role="status">
    <div><strong>{status}</strong><span>{description}</span></div>
    <progress aria-label={progressLabel} aria-valuetext={status} />
  </div>;
}

function JobStatus({ job, status, label }: { job: ProcessingJob | null; status: string; label: string }) {
  if (job?.state === "queued" || job?.state === "running") return <JobProgress job={job} />;
  return <StatusRow label={label} status={status} />;
}

function TranscriptView({ transcript, speakerMapping, speakerMappingLoading, canRenameSpeakers, renameSaving, onRenameSpeaker, onSeekToSegment, playbackUnavailableMessage, currentTime = 0 }: {
  transcript: Transcript;
  speakerMapping: SpeakerMapping | null;
  speakerMappingLoading: boolean;
  canRenameSpeakers: boolean;
  renameSaving: boolean;
  onRenameSpeaker: (speakerNumber: number, displayName: string | null) => Promise<void>;
  onSeekToSegment: (seconds: number) => void;
  playbackUnavailableMessage: string | null;
  currentTime?: number;
}) {
  const activeIndex = transcript.segments.findIndex(
    (segment) => currentTime >= segment.start_seconds && currentTime <= segment.end_seconds,
  );
  const activeSegment = useRef<HTMLLIElement>(null);
  const editButtonRef = useRef<HTMLButtonElement>(null);
  const returnFocusTarget = useRef<{ speakerNumber: number; segmentId: string } | null>(null);
  const restoreEditFocus = useRef(false);
  const [editing, setEditing] = useState<{ speakerNumber: number; segmentId: string } | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);

  useEffect(() => {
    activeSegment.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [activeIndex]);

  useLayoutEffect(() => {
    if (editing || !restoreEditFocus.current) return;
    restoreEditFocus.current = false;
    editButtonRef.current?.focus();
  }, [editing]);

  function beginEdit(speaker: ConversationSpeaker, segmentId: string) {
    returnFocusTarget.current = { speakerNumber: speaker.speaker_number, segmentId };
    setEditing({ speakerNumber: speaker.speaker_number, segmentId });
    setDraft(speaker.display_name ?? "");
    setRenameError(null);
  }

  function closeEdit() {
    restoreEditFocus.current = true;
    setEditing(null);
    setRenameError(null);
  }

  async function saveSpeakerName(event: FormEvent<HTMLFormElement>, speaker: ConversationSpeaker) {
    event.preventDefault();
    if (!canRenameSpeakers || saving || draft === (speaker.display_name ?? "")) return;
    setSaving(true);
    setRenameError(null);
    try {
      await onRenameSpeaker(speaker.speaker_number, draft.trim() || null);
      closeEdit();
    } catch (error) {
      setRenameError(error instanceof Error ? error.message : "The speaker name could not be saved.");
    } finally {
      setSaving(false);
    }
  }

  return <section className="transcript-section" aria-label="Transcript">
    <div className="section-heading"><h3>Transcript</h3><span>{transcript.model}</span></div>
    {playbackUnavailableMessage && <p className="transcript-playback-unavailable" id="transcript-playback-unavailable" role="status">{playbackUnavailableMessage}</p>}
    {speakerMappingLoading && <p className="speaker-loading" role="status">Loading speaker names…</p>}
    {transcript.segments.length === 0
      ? <p className="transcript-empty">No speech was detected.</p>
      : <ol className="transcript-list">{transcript.segments.map((segment, index) => {
        const speaker = speakerMapping?.speakers.find((item) => item.label === segment.speaker) ?? null;
        const displayName = speaker?.display_name || speaker?.label || segment.speaker;
        const editingThisSpeaker = Boolean(editing && speaker && editing.speakerNumber === speaker.speaker_number && editing.segmentId === segment.evidence_id);
        return <li
          aria-current={index === activeIndex ? "true" : undefined}
          className={`transcript-segment${index === activeIndex ? " active" : ""}`}
          key={segment.evidence_id}
          onClick={(event) => {
            if (playbackUnavailableMessage || (event.target as HTMLElement).closest("button, input, textarea, select, a, [contenteditable='true'], .speaker-rename-form")) return;
            onSeekToSegment(segment.start_seconds);
          }}
          ref={index === activeIndex ? activeSegment : undefined}
        >
          <button
            aria-describedby={playbackUnavailableMessage ? "transcript-playback-unavailable" : undefined}
            aria-label={`Play segment from ${formatTime(segment.start_seconds)}`}
            className="transcript-segment-seek"
            disabled={Boolean(playbackUnavailableMessage)}
            onClick={() => onSeekToSegment(segment.start_seconds)}
            title={playbackUnavailableMessage ?? undefined}
            type="button"
          ><time>{formatTime(segment.start_seconds)}</time></button>
          {editingThisSpeaker && speaker
            ? <form className="speaker-rename-form" onSubmit={(event) => void saveSpeakerName(event, speaker)}>
              <label htmlFor={`speaker-name-${speaker.speaker_number}`}>Name for {speaker.label}</label>
              <input
                autoFocus
                disabled={!canRenameSpeakers || saving || renameSaving}
                id={`speaker-name-${speaker.speaker_number}`}
                onChange={(event) => setDraft(event.currentTarget.value)}
                onKeyDown={(event) => {
                  if (event.key === "Escape" && !saving) {
                    event.preventDefault();
                    closeEdit();
                  }
                }}
                value={draft}
              />
              {renameError && <p className="error" role="alert">{renameError}</p>}
              {!canRenameSpeakers && <p role="status">Speaker names cannot be changed while transcription is active.</p>}
              <div>
                <button disabled={!canRenameSpeakers || saving || renameSaving || draft === (speaker.display_name ?? "")} type="submit">{saving || renameSaving ? "Saving…" : "Save name"}</button>
                <button className="secondary" disabled={saving || renameSaving} onClick={closeEdit} type="button">Cancel</button>
              </div>
            </form>
            : canRenameSpeakers && speaker
              ? <button
                className="speaker-name-edit"
                aria-label={`Edit speaker name for ${speaker.label}`}
                disabled={renameSaving}
                onClick={() => beginEdit(speaker, segment.evidence_id)}
                ref={returnFocusTarget.current?.speakerNumber === speaker.speaker_number && returnFocusTarget.current.segmentId === segment.evidence_id ? editButtonRef : undefined}
                type="button"
              >{displayName}</button>
              : <strong className="speaker-name">{displayName}</strong>}
          <p>{segment.text}</p>
        </li>;
      })}</ol>}
  </section>;
}

function SummaryView({ summary }: { summary: Summary }) {
  const hasContent = Boolean(summary.overview || summary.key_topics.length || summary.decisions.length || summary.action_items.length);
  return <section className="summary-section" aria-label="Summary">
    <div className="section-heading"><h3>Summary</h3><span>{summary.model}</span></div>
    {!hasContent && <p className="summary-empty">No summary content was produced.</p>}
    {summary.overview && <section className="summary-group">
      <h4>Overview</h4>
      <p>{summary.overview.text}</p>
    </section>}
    {summary.key_topics.length > 0 && <section className="summary-group">
      <h4>Key topics</h4>
      <ul>{summary.key_topics.map((item, index) => <li key={`topic-${index}`}>{item.text}</li>)}</ul>
    </section>}
    {summary.decisions.length > 0 && <section className="summary-group">
      <h4>Decisions</h4>
      <ul>{summary.decisions.map((item, index) => <li key={`decision-${index}`}>{item.text}</li>)}</ul>
    </section>}
    {summary.action_items.length > 0 && <section className="summary-group">
      <h4>Action items</h4>
      <ul>{summary.action_items.map((item, index) => <li key={`action-${index}`}>
        <span>{item.text}</span>
        {(item.owner || item.due_date) && <small>{[item.owner, item.due_date].filter(Boolean).join(" · ")}</small>}
      </li>)}</ul>
    </section>}
  </section>;
}

function MaintenancePage({ checking }: { checking: boolean }) {
  return <main className="maintenance-page">
    <section className="maintenance-card">
      <p className="eyebrow">Service status</p>
      <h1>{checking ? "Connecting to Muninn…" : "Maintenance"}</h1>
      <p>{checking ? "Checking the recording service." : "The recording service is temporarily unreachable."}</p>
      <p role="status">We’ll reconnect automatically. Your recordings will return when the API is reachable.</p>
      <span className="maintenance-spinner" aria-hidden="true" />
    </section>
  </main>;
}

function LiveWaveform({ analyser }: { analyser: AnalyserNode }) {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const element = canvas.current;
    const context = element?.getContext("2d");
    if (!element || !context) return;

    const samples = new Uint8Array(analyser.fftSize);
    let frame = 0;
    const draw = () => {
      analyser.getByteTimeDomainData(samples);
      context.clearRect(0, 0, element.width, element.height);
      context.beginPath();
      samples.forEach((sample, index) => {
        const x = (index / (samples.length - 1)) * element.width;
        const y = (sample / 255) * element.height;
        if (index === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      });
      context.strokeStyle = "#78b7df";
      context.lineWidth = 2;
      context.stroke();
      frame = requestAnimationFrame(draw);
    };
    frame = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(frame);
  }, [analyser]);

  return <canvas
    aria-label="Live recording waveform"
    className="recording-waveform"
    height={120}
    ref={canvas}
    role="img"
    width={600}
  />;
}

function RecordingLibrary({
  conversations,
  error,
  loading,
  importJob,
  recordingLocked,
  onImport,
  onRecord,
  onRetryImport,
  onSelect,
}: {
  conversations: Conversation[];
  error: string | null;
  loading: boolean;
  importJob: ImportJob | null;
  recordingLocked: boolean;
  onImport: (file: File) => void;
  onRecord: () => void;
  onRetryImport: () => void;
  onSelect: (item: Conversation) => void;
}) {
  const importInput = useRef<HTMLInputElement>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedTag, setSelectedTag] = useState("");
  const [selectedPerson, setSelectedPerson] = useState("");
  const [sortBy, setSortBy] = useState<RecordingSortKey>("date");
  const [sortAscending, setSortAscending] = useState(false);
  const tags = useMemo(
    () => [...new Set(conversations.flatMap((item) => item.tags))].sort((left, right) => left.localeCompare(right)),
    [conversations],
  );
  const people = useMemo(
    () => [...new Set(conversations.flatMap((item) => item.people))].sort((left, right) => left.localeCompare(right)),
    [conversations],
  );
  const visibleConversations = useMemo(() => {
    const query = searchQuery.trim().toLocaleLowerCase();
    const matches = conversations.filter((item) => {
      const searchableText = [item.name, item.description ?? "", ...item.tags, ...item.people].join(" ").toLocaleLowerCase();
      return (!query || searchableText.includes(query))
        && (!selectedTag || item.tags.includes(selectedTag))
        && (!selectedPerson || item.people.includes(selectedPerson));
    });
    const compare = (left: Conversation, right: Conversation) => sortBy === "name"
      ? left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: "base" })
      : Date.parse(left.occurred_at) - Date.parse(right.occurred_at);
    return matches.sort((left, right) => (sortAscending ? 1 : -1) * compare(left, right));
  }, [conversations, searchQuery, selectedPerson, selectedTag, sortAscending, sortBy]);
  const hasFilters = Boolean(searchQuery.trim() || selectedTag || selectedPerson);
  const activeFilterCount = Number(Boolean(selectedTag)) + Number(Boolean(selectedPerson));
  const sortOrderLabel = sortBy === "date"
    ? sortAscending ? "Oldest first" : "Newest first"
    : sortAscending ? "A–Z" : "Z–A";

  function clearFilters() {
    setSearchQuery("");
    setSelectedTag("");
    setSelectedPerson("");
  }

  function changeSortBy(value: string) {
    const next = value as RecordingSortKey;
    setSortBy(next);
    setSortAscending(next === "name");
  }

  return <section className="library-page">
    <header className="library-heading">
      <div>
        <p className="eyebrow">Your private archive</p>
        <h2>Recordings</h2>
        <p className="library-intro">Conversations, ready when you need them.</p>
      </div>
      <div className="library-actions">
        <button className="record-launch" aria-label="Start recording" disabled={recordingLocked} onClick={onRecord}>
          <span className="record-launch-dot" aria-hidden="true" />
          <span>Record</span>
        </button>
      </div>
    </header>
    {error && <p role="alert" className="error">{error}</p>}
    {importJob && <div className="import-status">
      <span role="status">{importJob.status === "creating" ? "Preparing import…" : importJob.status === "uploading" ? `Importing ${importJob.file.name}…` : "Import failed"}</span>
      {importJob.error && <p role="alert" className="error">{importJob.error}</p>}
      {importJob.status === "failed" && <button onClick={onRetryImport}>Retry import</button>}
    </div>}
    {loading && conversations.length === 0
      ? <section className="empty-library" aria-label="Loading recordings">
        <span className="maintenance-spinner" aria-hidden="true" />
        <h3>Loading recordings…</h3>
      </section>
      : conversations.length === 0
        ? <section className="empty-library">
          <div className="empty-wave" aria-hidden="true"><span /><span /><span /><span /><span /><span /><span /></div>
          <h3>Your recordings will live here</h3>
          <p>Press Record to capture a conversation. You can listen back and add notes afterward.</p>
        </section>
        : <section className="recording-library" aria-label="Saved recordings">
          <div className="section-heading"><h3>Recent recordings</h3><span>{visibleConversations.length} / {conversations.length}</span></div>
          <div className="recording-filters" aria-label="Filter and sort recordings">
            <label className="recording-search">Search
              <input
                aria-label="Search recordings"
                onChange={(event) => setSearchQuery(event.currentTarget.value)}
                placeholder="Name, description, tags, or people"
                type="search"
                value={searchQuery}
              />
            </label>
            <details className="recording-filter-panel">
              <summary>Filters{activeFilterCount > 0 ? ` (${activeFilterCount})` : ""}</summary>
              <div className="recording-filter-options">
                <label>Tag<select aria-label="Filter by tag" onChange={(event) => setSelectedTag(event.currentTarget.value)} value={selectedTag}>
                  <option value="">All tags</option>
                  {tags.map((tag) => <option key={tag} value={tag}>{tag}</option>)}
                </select></label>
                <label>People<select aria-label="Filter by people" onChange={(event) => setSelectedPerson(event.currentTarget.value)} value={selectedPerson}>
                  <option value="">All people</option>
                  {people.map((person) => <option key={person} value={person}>{person}</option>)}
                </select></label>
                <label>Sort by<select aria-label="Sort recordings by" onChange={(event) => changeSortBy(event.currentTarget.value)} value={sortBy}>
                  <option value="date">Date</option>
                  <option value="name">Name</option>
                </select></label>
                <button
                  aria-label={`Reverse sort order, currently ${sortOrderLabel}`}
                  className="sort-direction"
                  onClick={() => setSortAscending((ascending) => !ascending)}
                  type="button"
                >{sortOrderLabel}</button>
                {hasFilters && <button className="clear-filters" onClick={clearFilters} type="button">Clear filters</button>}
              </div>
            </details>
          </div>
          {visibleConversations.length === 0
            ? <div className="no-recordings-match" role="status">
              <p>No recordings match those filters.</p>
              <button className="secondary" onClick={clearFilters} type="button">Clear filters</button>
            </div>
            : <div className="recording-list">
              {visibleConversations.map((item) => <button aria-label={`Open recording ${item.name}`} className="recording-row" key={item.id} onClick={() => onSelect(item)}>
                <span className="recording-row-mark" aria-hidden="true"><span /><span /><span /><span /></span>
                <span className="recording-row-main">
                  <strong>{item.name}</strong>
                  <span>{new Date(item.occurred_at).toLocaleString()}</span>
                </span>
                <span className="recording-row-status">
                  <span className={item.recording_available ? "status-dot ready" : "status-dot pending"} />
                  {item.recording_available ? "Audio saved" : "Audio pending"}
                </span>
                <span className="recording-row-state">{item.sync_state.replaceAll("_", " ")}</span>
                <span className="recording-row-arrow" aria-hidden="true">›</span>
              </button>)}
            </div>}
        </section>}
    <div className="library-import-actions">
      <button className="import-launch" disabled={recordingLocked} onClick={() => importInput.current?.click()}>Import audio</button>
      <input
        accept={supportedAudioAccept}
        aria-label="Import audio file"
        hidden
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          if (file) onImport(file);
          event.currentTarget.value = "";
        }}
        ref={importInput}
        type="file"
      />
    </div>
  </section>;
}

function RecordingScreen({ capture, onBack }: { capture: ReturnType<typeof useAudioCapture>; onBack: () => void }) {
  const elapsed = `${String(Math.floor(capture.elapsedSeconds / 60)).padStart(2, "0")}:${String(capture.elapsedSeconds % 60).padStart(2, "0")}`;
  return <section className="detail-page capture-detail">
    {capture.state !== "recording" && <button className="back-link" onClick={onBack}>← Recordings</button>}
    <div className="detail-heading">
      <p className="eyebrow">Recording in progress</p>
      <h2>Stay in the moment</h2>
      <p>Your audio remains in this tab and uploads when you stop.</p>
    </div>
    <div className="live-recording-card">
      {capture.analyser && <LiveWaveform analyser={capture.analyser} />}
      {capture.state === "recording"
        ? <>
          <p className="recording-elapsed"><span className="live-dot" aria-hidden="true" /> Recording {elapsed}</p>
          <button className="stop-recording" aria-label="Stop recording" onClick={capture.stop}>Stop recording</button>
        </>
        : capture.error
          ? <p role="alert" className="error">{capture.error}</p>
          : <p role="status">Starting microphone…</p>}
    </div>
  </section>;
}

function RecordingDetail({
  recording,
  conversation,
  previewUrl,
  status,
  error,
  onRetry,
  onDiscard,
  onPlaybackTime,
  onPlaybackStateChange,
}: {
  recording: PendingRecording;
  conversation: Conversation | null;
  previewUrl: string | null;
  status: UploadStatus;
  error: string | null;
  onRetry: () => void;
  onDiscard: () => void;
  onPlaybackTime: (seconds: number) => void;
  onPlaybackStateChange: (playing: boolean) => void;
}) {
  return <section className="detail-page">
    <div className="detail-heading">
      <p className="eyebrow">New recording</p>
      <h2>{conversation?.name ?? "Browser recording"}</h2>
      <p>Recorded {new Date(recording.occurredAt).toLocaleString()}</p>
    </div>
    <div className="detail-card">
      {previewUrl
        ? <RecordingPlayer
          source={{ kind: "local", url: previewUrl }}
          notes={[]}
          onPlaybackTime={onPlaybackTime}
          onPlaybackStateChange={onPlaybackStateChange}
        />
        : <p role="status">Preparing audio preview…</p>}
      <StatusDisclosure conversation={conversation} uploadStatus={status} />
      <p className="file-size">{Math.max(1, Math.ceil(recording.blob.size / 1024))} KiB held in this tab</p>
      {error && <p role="alert" className="error">{error}</p>}
      {status === "failed" && <div className="detail-actions">
        <button onClick={onRetry}>Retry upload</button>
        <button className="secondary" onClick={onDiscard}>Discard recording</button>
      </div>}
    </div>
  </section>;
}

function RecordingPlayer({
  source,
  notes,
  onAddNote,
  onPlaybackTime,
  onPlaybackStateChange,
  onPlaybackFailureChange,
  initialTime = 0,
  seekRequest,
  transcriptSeekRequest,
  onTranscriptSeekHandled,
  onNoteMarkerClick,
  onStreamError,
  onStreamUsable,
  onSeekRequestHandled,
  resumePlaybackOnLoad = false,
}: {
  source: PlaybackSource;
  notes: Note[];
  onAddNote?: (offsetSeconds: number, text: string) => Promise<void>;
  onPlaybackTime?: (seconds: number) => void;
  onPlaybackStateChange?: (playing: boolean) => void;
  onPlaybackFailureChange?: (failed: boolean) => void;
  initialTime?: number;
  onSeekNote?: (note: Note) => void;
  seekRequest?: { noteId: string; offsetSeconds: number; requestId: number } | null;
  transcriptSeekRequest?: { offsetSeconds: number; requestId: number } | null;
  onTranscriptSeekHandled?: (requestId: number) => void;
  onNoteMarkerClick?: (note: Note) => void;
  onStreamError?: (currentTime: number, wasPlaying: boolean) => void;
  onStreamUsable?: () => void;
  onSeekRequestHandled?: (requestId: number) => void;
  resumePlaybackOnLoad?: boolean;
}) {
  const audio = useRef<HTMLAudioElement>(null);
  const resumePlaybackAfterComposer = useRef(false);
  const handledSeekRequest = useRef<number | null>(null);
  const pendingSeekTime = useRef<number | null>(null);
  const transcriptSeekRequestRef = useRef(transcriptSeekRequest);
  transcriptSeekRequestRef.current = transcriptSeekRequest;
  const onTranscriptSeekHandledRef = useRef(onTranscriptSeekHandled);
  onTranscriptSeekHandledRef.current = onTranscriptSeekHandled;
  const resumedPlaybackOnLoad = useRef(false);
  const requestedPlayAfterLoad = useRef(false);
  const markerTrack = useRef<HTMLDivElement>(null);
  const [activeNoteTooltip, setActiveNoteTooltip] = useState<string | null>(null);
  const [currentTime, setCurrentTime] = useState(initialTime);
  const [seekDraft, setSeekDraft] = useState<number | null>(null);
  const [duration, setDuration] = useState(0);
  const [playbackState, setPlaybackState] = useState<PlaybackState>("idle");
  const [playRequested, setPlayRequested] = useState(false);
  const [composerOpen, setComposerOpen] = useState(false);
  const [noteOffset, setNoteOffset] = useState<number | null>(null);
  const [noteSaveOffset, setNoteSaveOffset] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const noteSaveInFlight = useRef(false);
  const noteSaveOffsetAtSave = useRef<number | null>(null);
  const pointerScrub = useRef<{ pointerId: number; noteOffset: number | null; noteSaveOffset: number | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const maximum = Math.max(duration, 0.1);
  const playing = playbackState === "playing";
  const buffering = playbackState === "loading" || playbackState === "buffering";
  const activeTooltipNote = notes.find((note) => note.id === activeNoteTooltip) ?? null;

  function noteMarkerPosition(offsetSeconds: number): string {
    const width = markerTrack.current?.clientWidth ?? 0;
    const usableWidth = Math.max(0, width - 16);
    const fraction = duration > 0 ? Math.min(1, Math.max(0, offsetSeconds / duration)) : 0;
    return `${8 + fraction * usableWidth}px`;
  }

  function seek(seconds: number) {
    const player = audio.current;
    if (!player) return;
    pendingSeekTime.current = Math.abs(player.currentTime - seconds) > 0.001 ? seconds : null;
    player.currentTime = seconds;
    setCurrentTime(seconds);
    onPlaybackTime?.(seconds);
    if (player.paused || !playRequested) {
      setPlaybackState("paused");
      return;
    }
    setPlaybackState("buffering");
  }

  async function applyTranscriptSeek() {
    const request = transcriptSeekRequestRef.current;
    const player = audio.current;
    if (!request || !player || player.readyState < HTMLMediaElement.HAVE_METADATA) return;
    transcriptSeekRequestRef.current = null;
    const target = Number.isFinite(player.duration)
      ? Math.min(player.duration, Math.max(0, request.offsetSeconds))
      : Math.max(0, request.offsetSeconds);
    const shouldSeek = Math.abs(player.currentTime - target) > 0.001;
    pendingSeekTime.current = shouldSeek ? target : null;
    if (shouldSeek) {
      player.currentTime = target;
      setCurrentTime(target);
      onPlaybackTime?.(target);
    }
    setError(null);
    onPlaybackFailureChange?.(false);
    setPlayRequested(true);
    setPlaybackState(player.paused ? "loading" : shouldSeek ? "buffering" : "playing");
    try {
      await player.play();
      onPlaybackStateChange?.(true);
      if (!shouldSeek) setPlaybackState("playing");
    } catch {
      setPlayRequested(false);
      onPlaybackStateChange?.(false);
      onPlaybackFailureChange?.(true);
      setPlaybackState("failed");
      setError("The recording could not be loaded.");
    }
    onTranscriptSeekHandledRef.current?.(request.requestId);
  }

  function updateNoteOffset(seconds: number) {
    if (noteSaveInFlight.current) return;
    const offset = noteOffsetSeconds(seconds);
    setNoteOffset(offset);
    setNoteSaveOffset(offset);
  }

  function cancelPointerScrub(pointerId: number) {
    const scrub = pointerScrub.current;
    if (!scrub || scrub.pointerId !== pointerId) return;
    pointerScrub.current = null;
    setSeekDraft(null);
    if (!composerOpen) return;
    const offset = noteSaveInFlight.current ? noteSaveOffsetAtSave.current : scrub.noteOffset;
    const saveOffset = noteSaveInFlight.current ? noteSaveOffsetAtSave.current : scrub.noteSaveOffset;
    setNoteOffset(offset);
    setNoteSaveOffset(saveOffset);
  }

  useEffect(() => {
    if (!seekRequest || handledSeekRequest.current === seekRequest.requestId || !audio.current) return;
    handledSeekRequest.current = seekRequest.requestId;
    seek(seekRequest.offsetSeconds);
    onSeekRequestHandled?.(seekRequest.requestId);
  }, [onSeekRequestHandled, seekRequest]);

  useEffect(() => {
    if (!transcriptSeekRequest) return;
    transcriptSeekRequestRef.current = transcriptSeekRequest;
    void applyTranscriptSeek();
  }, [transcriptSeekRequest]);

  async function togglePlayback() {
    const player = audio.current;
    if (!player) return;
    setError(null);
    onPlaybackFailureChange?.(false);
    if (player.paused) {
      setPlayRequested(true);
      setPlaybackState("loading");
      try {
        if (player.networkState === HTMLMediaElement.NETWORK_NO_SOURCE) {
          requestedPlayAfterLoad.current = true;
          player.load();
          return;
        }
        await player.play();
        onPlaybackStateChange?.(true);
      } catch {
        setPlayRequested(false);
        onPlaybackStateChange?.(false);
        onPlaybackFailureChange?.(true);
        setPlaybackState("failed");
        setError("The recording could not be loaded.");
      }
    } else {
      resumePlaybackAfterComposer.current = false;
      setPlayRequested(false);
      onPlaybackStateChange?.(false);
      player.pause();
      setPlaybackState("paused");
    }
  }

  function closeNoteComposer() {
    const shouldResume = resumePlaybackAfterComposer.current;
    resumePlaybackAfterComposer.current = false;
    setComposerOpen(false);
    setNoteOffset(null);
    setNoteSaveOffset(null);
    setError(null);
    if (shouldResume && audio.current?.paused) void togglePlayback();
  }

  function openNoteComposer() {
    if (composerOpen) {
      closeNoteComposer();
      return;
    }
    const player = audio.current;
    resumePlaybackAfterComposer.current = Boolean(player && !player.paused && !player.ended);
    const offset = noteOffsetSeconds(player?.currentTime ?? currentTime);
    setNoteOffset(offset);
    setNoteSaveOffset(offset);
    player?.pause();
    setPlayRequested(false);
    setPlaybackState("paused");
    setDraft("");
    setError(null);
    setComposerOpen(true);
  }

  async function saveNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!onAddNote || noteOffset === null || !draft.trim() || noteSaveInFlight.current) return;
    const offset = noteSaveOffset ?? noteOffset;
    noteSaveInFlight.current = true;
    noteSaveOffsetAtSave.current = offset;
    setNoteOffset(offset);
    setNoteSaveOffset(offset);
    setSaving(true);
    setError(null);
    try {
      await onAddNote(offset, draft.trim());
      setDraft("");
      closeNoteComposer();
    } catch {
      setError("The note could not be saved.");
    } finally {
      noteSaveInFlight.current = false;
      noteSaveOffsetAtSave.current = null;
      setSaving(false);
    }
  }

  useEffect(() => () => {
    if (source.kind === "local" && source.url.startsWith("blob:")) URL.revokeObjectURL(source.url);
  }, []);

  return <section aria-label="Recording player" className={`recording-player playback-${playbackState}`} data-playback-source={source.kind}>
    <audio
      aria-hidden="true"
      className="audio-source"
      data-testid="recording-audio"
      onCanPlay={() => {
        onPlaybackFailureChange?.(false);
        setPlaybackState((state) => state === "loading" || state === "buffering" ? (playRequested ? "playing" : "paused") : state);
        if (source.kind === "stream") onStreamUsable?.();
      }}
      onDurationChange={(event) => setDuration(Number.isFinite(event.currentTarget.duration) ? event.currentTarget.duration : 0)}
      onEnded={() => {
        onPlaybackStateChange?.(false);
        setPlaybackState("ended");
      }}
      onError={(event) => {
        if (source.kind === "stream") onStreamError?.(pendingSeekTime.current ?? event.currentTarget.currentTime, playRequested);
        pendingSeekTime.current = null;
        setPlayRequested(false);
        onPlaybackStateChange?.(false);
        onPlaybackFailureChange?.(true);
        setPlaybackState("failed");
        setError("The recording could not be loaded.");
      }}

      onLoadedMetadata={(event) => {
        const player = event.currentTarget;
        setDuration(Number.isFinite(player.duration) ? player.duration : 0);
        if (initialTime > 0) player.currentTime = initialTime;
        if (transcriptSeekRequestRef.current) {
          void applyTranscriptSeek();
        } else if (resumePlaybackOnLoad && !resumedPlaybackOnLoad.current) {
          resumedPlaybackOnLoad.current = true;
          setPlayRequested(true);
          setPlaybackState("loading");
          void player.play().catch(() => {
            setPlayRequested(false);
            onPlaybackFailureChange?.(true);
            setPlaybackState("failed");
            setError("The recording could not be loaded.");
          });
        }
      }}
      onLoadStart={(event) => {
        setError(null);
        if (requestedPlayAfterLoad.current) {
          requestedPlayAfterLoad.current = false;
          setPlaybackState("loading");
          void event.currentTarget.play().catch(() => {
            setPlayRequested(false);
            onPlaybackFailureChange?.(true);
            setPlaybackState("failed");
            setError("The recording could not be loaded.");
          });
          return;
        }
        setPlayRequested(false);
        setPlaybackState("idle");
      }}
      onPause={() => {
        setPlayRequested(false);
        setPlaybackState((state) => state === "ended" ? state : "paused");
      }}
      onPlay={() => {
        setPlayRequested(true);
        onPlaybackFailureChange?.(false);
        onPlaybackStateChange?.(true);
      }}
      onPlaying={() => {
        setPlaybackState("playing");
        onPlaybackFailureChange?.(false);
        if (source.kind === "stream") onStreamUsable?.();
      }}
      onSeeked={() => {
        pendingSeekTime.current = null;
        setSeekDraft(null);
        if (source.kind === "stream") onStreamUsable?.();
        setPlaybackState((state) => state === "buffering" ? (playRequested ? "playing" : "paused") : state);
      }}
      onStalled={() => {
        if (playRequested) setPlaybackState("buffering");
      }}
      onTimeUpdate={(event) => {
        const time = event.currentTarget.currentTime;
        setCurrentTime(time);
        onPlaybackTime?.(time);
      }}
      onWaiting={() => {
        if (playRequested) setPlaybackState("buffering");
      }}
      preload={source.kind === "stream" ? "metadata" : "auto"}
      ref={audio}
      src={source.url}
    />
    <div className="player-controls">
      <button
        aria-label={playing ? "Pause recording" : buffering ? "Buffering recording" : playbackState === "failed" ? "Retry playback" : "Play recording"}
        className="play-toggle"
        disabled={buffering}
        onClick={() => void togglePlayback()}
      >{buffering ? <span aria-hidden="true" className="play-loading-spinner" /> : playing ? "Ⅱ" : "▶"}</button>
      <div className="player-readout"><strong>{buffering
        ? <span role="status">Buffering…</span>
        : playing ? "Playing recording" : "Listen to recording"}</strong><span>{formatTime(currentTime)} / {formatTime(duration)}</span></div>
    </div>
    <div className="timeline-wrap">
      <input
        aria-label="Recording timeline"
        className="timeline-range"
        disabled={!duration || saving}
        max={maximum}
        min={0}
        onChange={(event) => {
          if (noteSaveInFlight.current) return;
          const value = Number(event.currentTarget.value);
          setSeekDraft(value);
          if (composerOpen) updateNoteOffset(value);
          if (!pointerScrub.current && event.nativeEvent instanceof InputEvent && event.nativeEvent.inputType === "insertReplacementText") {
            seek(value);
            setSeekDraft(null);
          }
        }}
        onPointerDown={(event) => {
          if (noteSaveInFlight.current || event.button !== 0) return;
          pointerScrub.current = { pointerId: event.pointerId, noteOffset, noteSaveOffset };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerUp={(event) => {
          const scrub = pointerScrub.current;
          if (!scrub || scrub.pointerId !== event.pointerId) return;
          pointerScrub.current = null;
          const value = Number(event.currentTarget.value);
          if (noteSaveInFlight.current) {
            setSeekDraft(null);
            return;
          }
          if (composerOpen) updateNoteOffset(value);
          seek(value);
          setSeekDraft(null);
        }}
        onPointerCancel={(event) => cancelPointerScrub(event.pointerId)}
        onLostPointerCapture={(event) => cancelPointerScrub(event.pointerId)}
        onKeyUp={(event) => {
          if (!noteSaveInFlight.current && ["ArrowLeft", "ArrowRight", "Home", "End", "PageDown", "PageUp"].includes(event.key)) {
            const value = Number(event.currentTarget.value);
            if (composerOpen) updateNoteOffset(value);
            seek(value);
            setSeekDraft(null);
          }
        }}
        step={0.001}
        type="range"
        value={Math.min(seekDraft ?? currentTime, maximum)}
      />
      {duration > 0 && <div aria-label="Note markers" className="timeline-markers" ref={markerTrack}>
        {notes.map((note) => <button
          aria-describedby={activeNoteTooltip === note.id ? `timeline-note-tooltip-${note.id}` : undefined}
          aria-label={`Jump to note at ${formatTime(note.offset_seconds)}: ${note.text}`}
          className="timeline-marker"
          key={note.id}
          onBlur={(event) => {
            if (!event.currentTarget.matches(":hover")) {
              setActiveNoteTooltip((activeId) => activeId === note.id ? null : activeId);
            }
          }}
          onClick={() => {
            seek(note.offset_seconds);
            onNoteMarkerClick?.(note);
          }}
          onFocus={() => setActiveNoteTooltip(note.id)}
          onMouseEnter={() => setActiveNoteTooltip(note.id)}
          onMouseLeave={(event) => {
            if (document.activeElement !== event.currentTarget) {
              setActiveNoteTooltip((activeId) => activeId === note.id ? null : activeId);
            }
          }}
          style={{ left: noteMarkerPosition(note.offset_seconds) }}
          type="button"
        />)}
        {activeTooltipNote && <div
          className="timeline-note-tooltip"
          id={`timeline-note-tooltip-${activeTooltipNote.id}`}
          role="tooltip"
          style={{ left: `clamp(min(9rem, 50%), ${noteMarkerPosition(activeTooltipNote.offset_seconds)}, calc(100% - min(9rem, 50%)))` }}
        >{noteMarkerTitle(activeTooltipNote.text)}</div>}
      </div>}
    </div>
    {(notes.length > 0 || onAddNote) && <div className="player-actions">
      {notes.length > 0 && <span>{notes.length} timeline {notes.length === 1 ? "note" : "notes"}</span>}
      <div className="player-action-buttons">
        {onAddNote && <button
          aria-label="Add note"
          className="note-at-current"
          disabled={saving}
          onClick={openNoteComposer}
          title="Add a note while listening to pin a thought to this moment."
        >+ Add note</button>}
      </div>
    </div>}
    {composerOpen && onAddNote && <form className="note-composer" onSubmit={(event) => void saveNote(event)}>
      <label htmlFor="current-note">Note at {formatTime(noteOffset ?? currentTime)}</label>
      <textarea autoFocus disabled={saving} id="current-note" onChange={(event) => setDraft(event.currentTarget.value)} value={draft} />
      <div><button disabled={saving || !draft.trim()} type="submit">{saving ? "Saving…" : "Save note"}</button><button className="secondary" disabled={saving} onClick={closeNoteComposer} type="button">Cancel</button></div>
    </form>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>;
}

function NotesList({
  notes,
  error,
  canDelete,
  onDelete,
  onOpenNote,
  onSeekNote,
  currentTime = 0,
}: {
  notes: Note[];
  error: string | null;
  canDelete: boolean;
  onDelete: (noteId: string) => void;
  onOpenNote: (note: Note) => void;
  onSeekNote: (note: Note) => void;
  currentTime?: number;
}) {
  const activeNote = notes.reduce<Note | null>((closest, note) => {
    const distance = Math.abs(note.offset_seconds - currentTime);
    if (distance >= 5) return closest;
    if (!closest) return note;
    const closestDistance = Math.abs(closest.offset_seconds - currentTime);
    if (distance < closestDistance) return note;
    if (distance === closestDistance && note.offset_seconds <= currentTime && closest.offset_seconds > currentTime) return note;
    return closest;
  }, null);
  const activeNoteElement = useRef<HTMLElement>(null);

  useEffect(() => {
    activeNoteElement.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [activeNote?.id]);

  return <section className="notes-section">
    <div className="section-heading"><h3>Timeline notes</h3><span>{notes.length}</span></div>
    {error && <p role="alert" className="error">{error}</p>}
    {notes.length > 0 && <div className="notes-list">{notes.map((note) => <article
      aria-current={note.id === activeNote?.id ? "true" : undefined}
      className={`note-row${note.id === activeNote?.id ? " active" : ""}`}
      data-note-id={note.id}
      key={note.id}
      ref={note.id === activeNote?.id ? activeNoteElement : undefined}
    >
      <div className="note-open">
        <button
          aria-label={`Seek to note at ${formatTime(note.offset_seconds)}: ${note.text}`}
          className="note-seek-button"
          onClick={() => onSeekNote(note)}
          type="button"
        ><time>{formatTime(note.offset_seconds)}</time></button>
        <button
          aria-label={`Seek to note at ${formatTime(note.offset_seconds)}: ${note.text}`}
          className="note-text-button"
          onClick={() => onSeekNote(note)}
          type="button"
        ><span className="note-preview">{note.text}</span></button>
      </div>
      {note.author_kind === "agent" && <span className="note-author">Agent</span>}
      <div className="note-actions">
        {note.author_kind === "human" && <button
          aria-label={`Edit note at ${formatTime(note.offset_seconds)}`}
          className="note-icon-button note-edit-button"
          onClick={() => onOpenNote(note)}
          title="Edit note"
          type="button"
        ><span aria-hidden="true">✏️</span></button>}
        {canDelete && note.author_kind === "human" && <button
          aria-label={`Delete note at ${formatTime(note.offset_seconds)}`}
          className="note-icon-button note-delete-button"
          onClick={() => onDelete(note.id)}
          title="Delete note"
          type="button"
        ><span aria-hidden="true">🗑️</span></button>}
      </div>
    </article>)}</div>}
  </section>;
}

function Review({
  api,
  conversation,
  session,
  audioSource,
  uploadStatus,
  initialPlaybackTime,
  resumePlaybackOnLoad,
  onPlaybackHandoffComplete,
  onChanged,
  onDeleted,
}: {
  api: ApiClient;
  conversation: Conversation;
  session: AuthSession;
  audioSource: string | null;
  uploadStatus: UploadStatus | null;
  initialPlaybackTime: number;
  resumePlaybackOnLoad: boolean;
  onPlaybackHandoffComplete: () => void;
  onChanged: (item: Conversation) => void;
  onDeleted: () => void;
}) {
  const [notes, setNotes] = useState<Note[]>([]);
  const [notesError, setNotesError] = useState<string | null>(null);
  const [notesLoaded, setNotesLoaded] = useState(false);
  const [recordingGrant, setRecordingGrant] = useState<string | null>(null);
  const grantRefreshAttempted = useRef(false);
  const grantRefreshRequest = useRef(0);
  const resumePlaybackAfterGrantRefresh = useRef(false);
  const activeConversationId = useRef(conversation.id);
  const onChangedRef = useRef(onChanged);
  activeConversationId.current = conversation.id;
  onChangedRef.current = onChanged;
  const [downloadState, setDownloadState] = useState<"idle" | "downloading" | "downloaded" | "failed">("idle");
  const [artifactDownloadStates, setArtifactDownloadStates] = useState<Record<DownloadArtifact, ArtifactDownloadState>>({
    recording: "idle",
    transcript: "idle",
    notes: "idle",
    summary: "idle",
  });
  const [artifactDownloadError, setArtifactDownloadError] = useState<string | null>(null);
  const [playbackSource, setPlaybackSource] = useState<PlaybackSource | null>(null);
  const downloadedObjectUrl = useRef<string | null>(null);
  const playbackRequest = useRef<AbortController | null>(null);
  const [sourceState, setSourceState] = useState<SourcePhase>("resolving");
  const [playbackFailed, setPlaybackFailed] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [transcriptError, setTranscriptError] = useState<string | null>(null);
  const [speakerMapping, setSpeakerMapping] = useState<SpeakerMapping | null>(null);
  const [speakerMappingLoading, setSpeakerMappingLoading] = useState(conversation.transcript_available);
  const [speakerMappingError, setSpeakerMappingError] = useState<string | null>(null);
  const speakerMappingRequestSequence = useRef(0);
  const speakerRenameInFlight = useRef(false);
  const [speakerRenameSaving, setSpeakerRenameSaving] = useState(false);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const summaryRequestSequence = useRef(0);
  const transcriptRequestSequence = useRef(0);
  const refreshedTranscriptionJob = useRef<string | null>(null);
  const [summaryAvailable, setSummaryAvailable] = useState(conversation.summary_available);
  const [summaryStaging, setSummaryStaging] = useState(false);
  const [summaryStageError, setSummaryStageError] = useState<string | null>(null);
  const summaryStageInFlight = useRef(false);
  const [activeTab, setActiveTab] = useState<ReviewTab>("summary");
  const [playbackTime, setPlaybackTime] = useState(initialPlaybackTime);
  const [noteSeekRequest, setNoteSeekRequest] = useState<{ noteId: string; offsetSeconds: number; requestId: number } | null>(null);
  const noteSeekRequestCounter = useRef(0);
  const [transcriptSeekRequest, setTranscriptSeekRequest] = useState<{ offsetSeconds: number; requestId: number } | null>(null);
  const transcriptSeekRequestCounter = useRef(0);
  const reviewPanel = useRef<HTMLDivElement>(null);
  const pendingNoteJump = useRef<string | null>(null);
  const [jobs, setJobs] = useState<ProcessingJob[]>([]);
  const [jobsLoaded, setJobsLoaded] = useState(false);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const jobsRequestSequence = useRef(0);
  const jobsRequest = useRef<{ sequence: number; conversationId: string } | null>(null);
  const [staging, setStaging] = useState(false);
  const [stagingError, setStagingError] = useState<string | null>(null);
  const [transcriptionRetryPending, setTranscriptionRetryPending] = useState(false);
  const transcriptionRetryJobId = useRef<string | null>(null);
  const transcriptionStageInFlight = useRef(false);
  const autoStageConversation = useRef<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState<MetadataDraft>({
    name: conversation.name,
    description: conversation.description,
    people: conversation.people,
    tags: conversation.tags,
  });
  const dialog = useRef<HTMLDialogElement>(null);
  const noteDialog = useRef<HTMLDialogElement>(null);
  const actionsMenu = useRef<HTMLDivElement>(null);
  const actionsTrigger = useRef<HTMLButtonElement>(null);
  const downloadMenuTrigger = useRef<HTMLButtonElement>(null);
  const [actionsOpen, setActionsOpen] = useState(false);
  const [downloadMenuOpen, setDownloadMenuOpen] = useState(false);
  const [selectedNote, setSelectedNote] = useState<Note | null>(null);
  const [noteDraft, setNoteDraft] = useState("");
  const [noteSaving, setNoteSaving] = useState(false);
  const [noteEditError, setNoteEditError] = useState<string | null>(null);
  const can = (capability: Parameters<AuthSession["capabilities"]["has"]>[0]) => session.capabilities.has(capability);
  const canWriteMetadata = can("metadata:write");
  const canDeleteConversation = can("deletion:write");
  const canResummarize = can("processing:admin");
  const canDownloadArtifacts = can("context:read");
  const canDownloadRecording = canDownloadArtifacts && conversation.recording_available;
  const canDownloadTranscript = canDownloadArtifacts && conversation.transcript_available;
  const canDownloadNotes = canDownloadArtifacts && notesLoaded && !notesError && notes.length > 0;
  const canDownloadSummary = canDownloadArtifacts && summaryAvailable;
  const canEditNotes = can("notes:human:edit");
  const canEditSelectedNote = canEditNotes && selectedNote?.author_kind === "human";
  const transcriptJob = jobs.find((job) => job.kind === "transcription") ?? null;
  const summaryJob = jobs.find((job) => job.kind === "summarization") ?? null;
  const summaryJobPending = summaryJob?.state === "queued" || summaryJob?.state === "running";
  const transcriptionJobPending = transcriptJob?.state === "queued" || transcriptJob?.state === "running";
  const transcriptionRetryAvailable = canResummarize
    && conversation.recording_available
    && (transcriptJob?.state === "failed" || transcriptJob?.state === "cancelled" || Boolean(stagingError) || transcriptionRetryPending);
  const recordingStatus = uploadStatus === "failed" ? "FAILED" : conversation.recording_available ? "UPLOADED" : "NOT UPLOADED";
  const transcriptionStatus = transcriptJob?.state === "running"
    || transcriptJob?.state === "queued"
    || transcriptJob?.state === "failed"
    || transcriptJob?.state === "cancelled"
    ? jobStateLabel(transcriptJob.state)
    : conversation.transcript_available
      ? "COMPLETED"
      : stagingError ? "FAILED" : transcriptJob ? jobStateLabel(transcriptJob.state) : jobsError ? "UNAVAILABLE" : staging || !jobsLoaded ? "CHECKING" : "READY";
  const transcriptPlaybackUnavailableMessage = !audioSource
    ? "Transcript playback is unavailable because no recording is available."
    : sourceState === "failed" || (sourceState === "ready" && playbackFailed)
      ? "Transcript playback is unavailable. Retry recording playback to enable segment seeking."
      : null;
  const summaryStatus = summaryJob?.state === "running"
    || summaryJob?.state === "queued"
    || summaryJob?.state === "failed"
    || summaryJob?.state === "cancelled"
    ? jobStateLabel(summaryJob.state)
    : summaryAvailable ? "COMPLETED" : summaryJob ? jobStateLabel(summaryJob.state) : jobsError ? "UNAVAILABLE" : jobsLoaded ? "READY" : "CHECKING";
  const recordingIssue = uploadStatus === "failed"
    ? "Recording upload failed."
    : conversation.recording_available ? null : "No recording has been uploaded.";
  const transcriptIssue = transcriptJob?.state === "failed"
    ? `${conversation.transcript_available ? "The current transcript is available, but the latest attempt failed. " : ""}${transcriptJob.last_error_details ?? "No failure details were recorded."}`
    : transcriptJob?.state === "cancelled"
      ? `${conversation.transcript_available ? "The current transcript is available, but the latest attempt was cancelled. " : ""}${transcriptJob.last_error_details ?? "The transcription attempt was cancelled."}`
      : stagingError ?? jobsError;
  const summaryIssue = summaryJob?.state === "failed"
    ? `${summaryAvailable ? "The current summary is available, but the latest attempt failed. " : ""}${summaryJob.last_error_details ?? "No failure details were recorded."}`
    : summaryJob?.state === "cancelled"
      ? `${summaryAvailable ? "The current summary is available, but the latest attempt was cancelled. " : ""}${summaryJob.last_error_details ?? "The summarization attempt was cancelled."}`
      : jobsError;
  const loadNotes = useCallback(async () => {
    setNotesLoaded(false);
    try {
      setNotes(await api.listNotes(conversation.id));
      setNotesError(null);
    } catch {
      setNotesError("Notes are unavailable.");
    } finally {
      setNotesLoaded(true);
    }
  }, [api, conversation.id]);

  useEffect(() => { void loadNotes(); }, [loadNotes]);

  useEffect(() => {
    let active = true;
    grantRefreshRequest.current += 1;
    playbackRequest.current?.abort();
    playbackRequest.current = null;
    if (downloadedObjectUrl.current) URL.revokeObjectURL(downloadedObjectUrl.current);
    downloadedObjectUrl.current = null;
    setDownloadState("idle");
    setSourceState("resolving");
    setPlaybackFailed(false);
    setSourceError(null);
    grantRefreshAttempted.current = false;
    setRecordingGrant(null);
    setPlaybackSource(null);
    if (!audioSource) {
      setSourceState("failed");
      setSourceError("No recording is available for playback.");
      return () => { active = false; };
    }
    if (audioSource.startsWith("blob:")) {
      setPlaybackSource({ kind: "local", url: audioSource });
      setSourceState("ready");
      return () => { active = false; };
    }
    setSourceState("resolving");
    void api.recordingAccessGrant(conversation.id).then((grant) => {
      if (!active) return;
      setRecordingGrant(grant);
      setPlaybackSource({ kind: "stream", url: api.recordingUrl(conversation.id, false, undefined, grant) });
      setSourceState("ready");
    }).catch(() => {
      if (!active) return;
      setSourceState("failed");
      setSourceError("Recording playback is unavailable.");
    });
    return () => { active = false; };
  }, [api, audioSource, conversation.id]);

  const loadJobs = useCallback(async () => {
    const conversationId = conversation.id;
    if (jobsRequest.current?.conversationId === conversationId) return;
    const request = { sequence: ++jobsRequestSequence.current, conversationId };
    jobsRequest.current = request;
    try {
      const result = await api.listJobs(conversationId);
      if (jobsRequest.current !== request || request.sequence !== jobsRequestSequence.current || activeConversationId.current !== conversationId) return;
      setJobs(result);
      setJobsLoaded(true);
      setJobsError(null);
    } catch {
      if (jobsRequest.current === request && request.sequence === jobsRequestSequence.current && activeConversationId.current === conversationId) {
        setJobsLoaded(true);
        setJobsError("Job status is unavailable. Retrying…");
      }
    } finally {
      if (jobsRequest.current === request) jobsRequest.current = null;
    }
  }, [api, conversation.id]);

  const loadSpeakerMapping = useCallback(async (preserveMapping?: SpeakerMapping) => {
    const conversationId = conversation.id;
    const requestId = ++speakerMappingRequestSequence.current;
    if (!conversation.transcript_available) {
      setSpeakerMapping(null);
      setSpeakerMappingLoading(false);
      setSpeakerMappingError(null);
      return null;
    }
    setSpeakerMappingLoading(true);
    setSpeakerMappingError(null);
    try {
      const result = await api.getSpeakers(conversationId);
      if (requestId !== speakerMappingRequestSequence.current || activeConversationId.current !== conversationId) return null;
      setSpeakerMapping(result);
      setSpeakerMappingLoading(false);
      setSpeakerMappingError(null);
      return result;
    } catch {
      if (requestId === speakerMappingRequestSequence.current && activeConversationId.current === conversationId) {
        setSpeakerMapping(preserveMapping ?? null);
        setSpeakerMappingLoading(false);
        setSpeakerMappingError("Speaker names could not be loaded.");
      }
      return null;
    }
  }, [api, conversation.id, conversation.transcript_available]);

  const loadSummary = useCallback(async (failureMessage: string) => {
    const requestId = ++summaryRequestSequence.current;
    const conversationId = conversation.id;
    try {
      const result = await api.getSummary(conversationId);
      if (requestId !== summaryRequestSequence.current || activeConversationId.current !== conversationId) return;
      setSummary(result);
      setSummaryAvailable(true);
      setSummaryError(null);
    } catch {
      if (requestId === summaryRequestSequence.current && activeConversationId.current === conversationId) {
        setSummaryError(failureMessage);
      }
    }
  }, [api, conversation.id]);

  const downloadForPlayback = useCallback(async (grant: string) => {
    playbackRequest.current?.abort();
    const request = new AbortController();
    playbackRequest.current = request;
    setSourceError(null);
    setDownloadState("downloading");
    try {
      const blob = await api.downloadRecording(conversation.id, grant, request.signal);
      if (request.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      if (downloadedObjectUrl.current) URL.revokeObjectURL(downloadedObjectUrl.current);
      downloadedObjectUrl.current = url;
      setPlaybackSource({ kind: "local", url });
      setDownloadState("downloaded");
    } catch (cause) {
      if (request.signal.aborted) return;
      setDownloadState("failed");
      setSourceError(cause instanceof Error ? cause.message : "The recording could not be downloaded.");
    } finally {
      if (playbackRequest.current === request) playbackRequest.current = null;
    }
  }, [api, conversation.id]);

  async function downloadArtifact(kind: DownloadArtifact) {
    const labels: Record<DownloadArtifact, string> = {
      recording: "Recording",
      transcript: "Transcript",
      notes: "Notes",
      summary: "Summary",
    };
    setArtifactDownloadStates((current) => ({ ...current, [kind]: "downloading" }));
    setArtifactDownloadError(null);
    try {
      if (kind === "recording") {
        const grant = await api.recordingAccessGrant(conversation.id);
        startNativeDownload(api.recordingUrl(conversation.id, true, undefined, grant));
        setArtifactDownloadStates((current) => ({ ...current, [kind]: "initiated" }));
        return;
      }
      const content = kind === "transcript"
        ? await api.getTranscript(conversation.id)
        : kind === "notes"
          ? await api.exportNotes(conversation.id)
          : await api.getSummary(conversation.id);
      const blob = new Blob([`${JSON.stringify(content, null, 2)}\n`], { type: "application/json;charset=utf-8" });
      saveBlob(blob, jsonDownloadFilename(conversation.occurred_at, conversation.name, kind));
      setArtifactDownloadStates((current) => ({ ...current, [kind]: "downloaded" }));
    } catch {
      setArtifactDownloadStates((current) => ({ ...current, [kind]: "failed" }));
      setArtifactDownloadError(`${labels[kind]} could not be downloaded.`);
    }
  }

  const refreshPlaybackGrant = useCallback(async (currentTime: number, wasPlaying: boolean) => {
    if (!recordingGrant) return;
    if (grantRefreshAttempted.current) {
      setSourceError("Recording playback is unavailable.");
      setSourceState("failed");
      return;
    }
    grantRefreshAttempted.current = true;
    const requestId = ++grantRefreshRequest.current;
    const conversationId = conversation.id;
    resumePlaybackAfterGrantRefresh.current = wasPlaying;
    setPlaybackTime(Number.isFinite(currentTime) ? currentTime : 0);
    setSourceError(null);
    setSourceState("resolving");
    try {
      const grant = await api.recordingAccessGrant(conversationId);
      if (requestId !== grantRefreshRequest.current || conversationId !== activeConversationId.current) return;
      setRecordingGrant(grant);
      setPlaybackSource({ kind: "stream", url: api.recordingUrl(conversationId, false, undefined, grant) });
      setPlaybackFailed(false);
      setSourceState("ready");
    } catch {
      if (requestId !== grantRefreshRequest.current || conversationId !== activeConversationId.current) return;
      setSourceError("Recording playback is unavailable.");
      setSourceState("failed");
    }
  }, [api, conversation.id, recordingGrant]);

  const retryPlaybackGrant = useCallback(() => {
    grantRefreshAttempted.current = false;
    void refreshPlaybackGrant(playbackTime, resumePlaybackAfterGrantRefresh.current);
  }, [playbackTime, refreshPlaybackGrant]);

  const retryInitialPlaybackGrant = useCallback(async () => {
    const requestId = ++grantRefreshRequest.current;
    const conversationId = conversation.id;
    setSourceError(null);
    setSourceState("resolving");
    try {
      const grant = await api.recordingAccessGrant(conversationId);
      if (requestId !== grantRefreshRequest.current || conversationId !== activeConversationId.current) return;
      grantRefreshAttempted.current = false;
      setRecordingGrant(grant);
      setPlaybackSource({ kind: "stream", url: api.recordingUrl(conversationId, false, undefined, grant) });
      setPlaybackFailed(false);
      setSourceState("ready");
    } catch {
      if (requestId !== grantRefreshRequest.current || conversationId !== activeConversationId.current) return;
      setSourceError("Recording playback is unavailable.");
      setSourceState("failed");
    }
  }, [api, conversation.id]);

  const stageTranscription = useCallback(async (isRetry = false) => {
    if (!canResummarize || !conversation.recording_available || transcriptionJobPending || transcriptionStageInFlight.current) return;
    transcriptionStageInFlight.current = true;
    if (isRetry) setTranscriptionRetryPending(true);
    setStaging(true);
    setStagingError(null);
    try {
      const job = await api.stageTranscription(conversation.id);
      if (isRetry) transcriptionRetryJobId.current = job.id;
      jobsRequestSequence.current += 1;
      setJobs((current) => [job, ...current.filter((existing) => existing.id !== job.id)]);
      setJobsLoaded(true);
    } catch (cause) {
      if (isRetry) setTranscriptionRetryPending(false);
      setStagingError(cause instanceof ApiError && cause.message.trim()
        ? cause.message
        : "The transcript job could not be queued. Try again.");
    } finally {
      transcriptionStageInFlight.current = false;
      setStaging(false);
    }
  }, [api, canResummarize, conversation.id, conversation.recording_available, transcriptionJobPending]);

  const stageSummarization = useCallback(async () => {
    if (!canResummarize || !conversation.transcript_available || summaryJobPending || summaryStageInFlight.current) return;
    summaryStageInFlight.current = true;
    setSummaryStaging(true);
    setSummaryStageError(null);
    try {
      const job = await api.stageSummarization(conversation.id);
      jobsRequestSequence.current += 1;
      setJobs((current) => [job, ...current.filter((existing) => existing.id !== job.id)]);
      setJobsLoaded(true);
    } catch (cause) {
      setSummaryStageError(cause instanceof ApiError && cause.message.trim()
        ? cause.message
        : "Re-summarization could not be queued. Try again.");
    } finally {
      summaryStageInFlight.current = false;
      setSummaryStaging(false);
    }
  }, [api, canResummarize, conversation.id, conversation.transcript_available, summaryJobPending]);

  const renameSpeaker = useCallback(async (speakerNumber: number, displayName: string | null) => {
    if (!canWriteMetadata) throw new Error("You do not have permission to rename speakers.");
    if (staging || transcriptionJobPending || transcriptionStageInFlight.current) {
      throw new Error("Wait for transcription to finish before renaming speakers.");
    }
    if (!speakerMapping) throw new Error("Speaker names are still loading. Try again shortly.");
    if (speakerRenameInFlight.current) throw new Error("A speaker name is already being saved.");
    const conversationId = conversation.id;
    speakerRenameInFlight.current = true;
    setSpeakerRenameSaving(true);
    try {
      const result = await api.renameSpeaker(conversationId, speakerMapping.speaker_revision, speakerNumber, displayName);
      if (activeConversationId.current !== conversationId) return;
      setSpeakerMapping(result);
      setSpeakerMappingError(null);
      if (result.summary_job) {
        summaryRequestSequence.current += 1;
        setSummary(null);
        setSummaryAvailable(false);
        setSummaryError(null);
        setSummaryStageError(null);
        jobsRequestSequence.current += 1;
        setJobs((current) => [result.summary_job!, ...current.filter((job) => job.id !== result.summary_job?.id)]);
        setJobsLoaded(true);
      }
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        const refreshedMapping = await loadSpeakerMapping(speakerMapping);
        if (!refreshedMapping) {
          throw new Error("Speaker names could not be reloaded after the conflict. Your draft is preserved; try again when the connection is restored.");
        }
        throw new Error("Speaker names changed elsewhere. Current names have been reloaded; review and save again.");
      }
      if (cause instanceof ApiError && cause.status === 404) {
        const requestId = ++transcriptRequestSequence.current;
        await Promise.all([
          loadSpeakerMapping(),
          api.getTranscript(conversationId).then((updatedTranscript) => {
            if (requestId === transcriptRequestSequence.current && activeConversationId.current === conversationId) {
              setTranscript(updatedTranscript);
              setTranscriptError(null);
            }
          }).catch(() => {
            if (activeConversationId.current === conversationId) setTranscriptError("The current transcript could not be refreshed.");
          }),
          api.getConversation(conversationId).then((updatedConversation) => {
            if (activeConversationId.current !== conversationId) return;
            onChangedRef.current(updatedConversation);
            setSummaryAvailable(updatedConversation.summary_available);
            if (!updatedConversation.summary_available) {
              summaryRequestSequence.current += 1;
              setSummary(null);
              setSummaryError(null);
            } else {
              void loadSummary("The current summary could not be refreshed.");
            }
          }).catch(() => undefined),
        ]);
        setSpeakerMappingError("That speaker is no longer in the current transcript. The transcript and speaker names were refreshed.");
        throw new Error("That speaker is no longer in the current transcript. The transcript and speaker names were refreshed.");
      }
      throw new Error(cause instanceof ApiError && cause.message.trim() ? cause.message : "The speaker name could not be saved.");
    } finally {
      speakerRenameInFlight.current = false;
      setSpeakerRenameSaving(false);
    }
  }, [api, canWriteMetadata, conversation.id, loadSpeakerMapping, loadSummary, speakerMapping, staging, transcriptionJobPending]);

  useEffect(() => {
    void loadJobs();
    const timer = window.setInterval(() => { void loadJobs(); }, 5000);
    return () => window.clearInterval(timer);
  }, [loadJobs]);

  useEffect(() => () => {
    jobsRequestSequence.current += 1;
    jobsRequest.current = null;
    grantRefreshRequest.current += 1;
    playbackRequest.current?.abort();
    if (downloadedObjectUrl.current) URL.revokeObjectURL(downloadedObjectUrl.current);
    downloadedObjectUrl.current = null;
  }, [conversation.id]);

  useEffect(() => {
    if (!actionsOpen) {
      setDownloadMenuOpen(false);
      return;
    }
    actionsMenu.current?.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')?.focus();
    const dismissOutside = (event: PointerEvent) => {
      if (!actionsMenu.current?.contains(event.target as Node) && !actionsTrigger.current?.contains(event.target as Node)) {
        setActionsOpen(false);
      }
    };
    const dismissWithEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setActionsOpen(false);
        actionsTrigger.current?.focus();
      }
    };
    document.addEventListener("pointerdown", dismissOutside);
    document.addEventListener("keydown", dismissWithEscape);
    return () => {
      document.removeEventListener("pointerdown", dismissOutside);
      document.removeEventListener("keydown", dismissWithEscape);
    };
  }, [actionsOpen]);

  useEffect(() => {
    if (downloadMenuOpen) {
      document.getElementById("conversation-download-menu")?.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')?.focus();
    }
  }, [downloadMenuOpen]);

  useEffect(() => {
    if (activeTab === "notes" && pendingNoteJump.current) {
      const target = pendingNoteJump.current;
      const element = reviewPanel.current?.querySelector<HTMLElement>(`[data-note-id="${target}"]`);
      if (element) {
        element.scrollIntoView({ behavior: "smooth", block: "center" });
        pendingNoteJump.current = null;
        return;
      }
    }
    reviewPanel.current?.scrollTo({ top: 0, behavior: "auto" });
  }, [activeTab, notes]);

  const seekToNote = useCallback((note: Note) => {
    setPlaybackTime(note.offset_seconds);
    setNoteSeekRequest({ noteId: note.id, offsetSeconds: note.offset_seconds, requestId: ++noteSeekRequestCounter.current });
  }, []);

  const markNoteSeekHandled = useCallback((requestId: number) => {
    setNoteSeekRequest((current) => current?.requestId === requestId ? null : current);
  }, []);

  const seekToTranscriptSegment = useCallback((offsetSeconds: number) => {
    if (!audioSource || sourceState === "failed" || (sourceState === "ready" && playbackFailed)) return;
    setPlaybackTime(offsetSeconds);
    setTranscriptSeekRequest({ offsetSeconds, requestId: ++transcriptSeekRequestCounter.current });
  }, [audioSource, playbackFailed, sourceState]);

  const markTranscriptSeekHandled = useCallback((requestId: number) => {
    setTranscriptSeekRequest((current) => current?.requestId === requestId ? null : current);
  }, []);

  const markStreamUsable = useCallback(() => {
    grantRefreshAttempted.current = false;
    resumePlaybackAfterGrantRefresh.current = false;
    setPlaybackFailed(false);
  }, []);

  const openTimelineNote = useCallback((note: Note) => {
    pendingNoteJump.current = note.id;
    seekToNote(note);
    setActiveTab("notes");
  }, [seekToNote]);

  useEffect(() => {
    if (
      !jobsLoaded
      || jobsError
      || jobs.length > 0
      || !conversation.recording_available
      || conversation.transcript_available
      || !session.capabilities.has("processing:admin")
      || autoStageConversation.current === conversation.id
    ) return;
    autoStageConversation.current = conversation.id;
    void stageTranscription();
  }, [conversation.id, conversation.recording_available, conversation.transcript_available, jobs.length, jobsError, jobsLoaded, session.capabilities, stageTranscription]);

  useEffect(() => {
    void loadSpeakerMapping();
    return () => { speakerMappingRequestSequence.current += 1; };
  }, [loadSpeakerMapping]);

  useEffect(() => {
    if (!conversation.transcript_available) {
      transcriptRequestSequence.current += 1;
      setTranscript(null);
      setTranscriptError(null);
      return;
    }
    let active = true;
    const requestId = ++transcriptRequestSequence.current;
    void api.getTranscript(conversation.id).then((result) => {
      if (!active || requestId !== transcriptRequestSequence.current) return;
      setTranscript(result);
      setTranscriptError(null);
    }).catch(() => {
      if (active && requestId === transcriptRequestSequence.current) setTranscriptError("The transcript could not be loaded.");
    });
    return () => { active = false; };
  }, [api, conversation.id, conversation.transcript_available]);

  useEffect(() => {
    if (transcriptionRetryPending
      && transcriptJob?.id === transcriptionRetryJobId.current
      && (transcriptJob.state === "succeeded" || transcriptJob.state === "failed" || transcriptJob.state === "cancelled")) {
      setTranscriptionRetryPending(false);
    }
  }, [transcriptionRetryPending, transcriptJob?.id, transcriptJob?.state]);

  useEffect(() => {
    if (transcriptJob?.state !== "succeeded" || refreshedTranscriptionJob.current === transcriptJob.id) return;
    refreshedTranscriptionJob.current = transcriptJob.id;
    let active = true;
    const conversationId = conversation.id;
    const refresh = () => {
      const requestId = ++transcriptRequestSequence.current;
      void api.getTranscript(conversationId).then((result) => {
        if (!active || requestId !== transcriptRequestSequence.current || activeConversationId.current !== conversationId) return;
        setTranscript(result);
        setTranscriptError(null);
        void loadSpeakerMapping();
      }).catch(() => {
        if (active && activeConversationId.current === conversationId) setTranscriptError("The completed transcript could not be refreshed.");
      });
    };
    if (conversation.transcript_available) {
      refresh();
    } else {
      void api.getConversation(conversationId).then((updatedConversation) => {
        if (!active || activeConversationId.current !== conversationId) return;
        onChangedRef.current(updatedConversation);
        if (updatedConversation.transcript_available) refresh();
      }).catch(() => {
        if (active && activeConversationId.current === conversationId) setTranscriptError("The completed transcript could not be refreshed.");
      });
    }
    return () => { active = false; };
  }, [api, conversation.id, conversation.transcript_available, loadSpeakerMapping, transcriptJob?.id, transcriptJob?.state]);

  useEffect(() => {
    setSummaryAvailable(conversation.summary_available);
  }, [conversation.id, conversation.summary_available]);

  useEffect(() => {
    if (!conversation.summary_available) {
      summaryRequestSequence.current += 1;
      setSummary(null);
      setSummaryError(null);
      return;
    }
    void loadSummary("The summary could not be loaded.");
  }, [conversation.id, conversation.summary_available, loadSummary]);

  useEffect(() => {
    if (summaryJob?.state === "succeeded") void loadSummary("The completed summary could not be refreshed.");
  }, [loadSummary, summaryJob?.id, summaryJob?.state]);

  useEffect(() => () => {
    summaryRequestSequence.current += 1;
  }, []);

  function openEdit() {
    setDraft({ name: conversation.name, description: conversation.description, people: conversation.people, tags: conversation.tags });
    setEditError(null);
    dialog.current?.showModal();
  }

  async function saveMetadata(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setEditError(null);
    try {
      onChanged(await api.updateConversation(conversation, draft));
      dialog.current?.close();
    } catch (caught) {
      setEditError(messageFor(caught));
    } finally {
      setSaving(false);
    }
  }

  function openNoteDetails(note: Note) {
    setSelectedNote(note);
    setNoteDraft(note.text);
    setNoteEditError(null);
    noteDialog.current?.showModal();
  }

  async function saveEditedNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedNote || !canEditSelectedNote || !noteDraft.trim()) return;
    setNoteSaving(true);
    setNoteEditError(null);
    try {
      const updated = await api.updateNote(conversation.id, selectedNote.id, noteDraft.trim());
      setNotes((current) => current.map((note) => note.id === updated.id ? updated : note));
      noteDialog.current?.close();
    } catch {
      setNoteEditError("The note could not be updated.");
    } finally {
      setNoteSaving(false);
    }
  }

  async function addNote(offsetSeconds: number, text: string) {
    const note = await api.createNote(conversation.id, offsetSeconds, text);
    setNotes((current) => [...current, note].sort((left, right) => left.offset_seconds - right.offset_seconds));
  }

  async function deleteNote(noteId: string) {
    try {
      await api.deleteNote(conversation.id, noteId);
      setNotes((current) => current.filter((note) => note.id !== noteId));
    } catch {
      setNotesError("The note could not be deleted.");
    }
  }

  function handleActionMenuKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    const activeMenu = (event.target as HTMLElement).closest<HTMLDivElement>('[role="menu"]');
    if (!activeMenu) return;
    if (event.key === "ArrowRight" && event.target === downloadMenuTrigger.current) {
      event.preventDefault();
      setDownloadMenuOpen(true);
      return;
    }
    if (event.key === "ArrowLeft" && activeMenu.id === "conversation-download-menu") {
      event.preventDefault();
      setDownloadMenuOpen(false);
      downloadMenuTrigger.current?.focus();
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const items = [...activeMenu.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')]
      .filter((item) => item.closest('[role="menu"]') === activeMenu);
    const currentIndex = items.indexOf(document.activeElement as HTMLButtonElement);
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? items.length - 1
        : (currentIndex + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items[nextIndex]?.focus();
  }

  function handleReviewTabKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, tab: ReviewTab) {
    const tabs: ReviewTab[] = ["summary", "transcript", "notes", "status"];
    const currentIndex = tabs.indexOf(tab);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = tabs.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const nextTab = tabs[nextIndex];
    setActiveTab(nextTab);
    document.getElementById(`review-tab-${nextTab}`)?.focus();
  }

  async function remove() {
    setError(null);
    if (!window.confirm("Permanently delete this conversation and every artifact?")) {
      actionsTrigger.current?.focus();
      return;
    }
    try {
      await api.deleteConversation(conversation.id);
      onDeleted();
    } catch {
      setError("Deletion failed.");
    }
  }

  return <section className="detail-page conversation-review">
    <div className="detail-heading">
      <div className="detail-title-row">
        <div className="detail-title-copy">
          <p className="eyebrow">Conversation details</p>
          <div className="conversation-title-line">
            <h1>{conversation.name}</h1>
            {(canWriteMetadata || canDeleteConversation || canResummarize || canDownloadArtifacts) && <div className="conversation-actions">
              <button
                aria-controls={actionsOpen ? "conversation-actions-menu" : undefined}
                aria-expanded={actionsOpen}
                aria-haspopup="menu"
                aria-label="Conversation actions"
                className="conversation-actions-trigger"
                onClick={() => setActionsOpen((open) => !open)}
                ref={actionsTrigger}
                type="button"
              ><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94s-.02-.64-.07-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.61-.22l-2.39.96a7.3 7.3 0 0 0-1.63-.95l-.36-2.54a.49.49 0 0 0-.49-.41h-3.84a.49.49 0 0 0-.49.41l-.36 2.54c-.59.23-1.13.55-1.63.95l-2.39-.96a.49.49 0 0 0-.61.22L2.65 8.84a.5.5 0 0 0 .12.64L4.8 11.06a6.3 6.3 0 0 0 0 1.88l-2.03 1.58a.5.5 0 0 0-.12.64l1.92 3.32c.12.21.37.29.61.22l2.39-.96c.5.4 1.04.72 1.63.95l.36 2.54c.04.24.25.41.49.41h3.84c.24 0 .45-.17.49-.41l.36-2.54c.59-.23 1.13-.55 1.63-.95l2.39.96c.24.09.49 0 .61-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58ZM12 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7Z" /></svg></button>
              {actionsOpen && <div
                aria-label="Conversation actions"
                className="conversation-actions-menu"
                id="conversation-actions-menu"
                onBlur={(event) => {
                  if (!event.currentTarget.contains(event.relatedTarget as Node) && event.relatedTarget !== actionsTrigger.current) setActionsOpen(false);
                }}
                onKeyDown={handleActionMenuKeyDown}
                ref={actionsMenu}
                role="menu"
              >
                {canWriteMetadata && <button
                  onClick={() => { setActionsOpen(false); openEdit(); }}
                  role="menuitem"
                  type="button"
                >Edit details</button>}
                {canDeleteConversation && <button
                  className="conversation-delete-action"
                  onClick={() => { setActionsOpen(false); void remove(); }}
                  role="menuitem"
                  type="button"
                >Delete conversation</button>}
                {canResummarize && <button
                  disabled={!conversation.transcript_available || summaryStaging || summaryJobPending}
                  onClick={() => { setActionsOpen(false); actionsTrigger.current?.focus(); void stageSummarization(); }}
                  role="menuitem"
                  title={conversation.transcript_available ? undefined : "A current transcript is required to re-summarize."}
                  type="button"
                >{summaryStaging
                  ? "Queueing re-summarization…"
                  : summaryJob?.state === "queued"
                    ? "Summarization queued"
                  : summaryJob?.state === "running"
                    ? "Summarizing…"
                    : "Re-summarize"}</button>}
                {transcriptionRetryAvailable && <button
                  disabled={staging || transcriptionJobPending}
                  onClick={() => { setActionsOpen(false); actionsTrigger.current?.focus(); void stageTranscription(true); }}
                  role="menuitem"
                  type="button"
                >{staging
                  ? "Queueing transcription…"
                  : transcriptJob?.state === "queued"
                    ? "Transcription queued"
                    : transcriptJob?.state === "running"
                      ? "Transcribing…"
                      : "Retry transcription"}</button>}
                {canDownloadArtifacts && <div className="conversation-download-group">
                  <button
                    aria-controls={downloadMenuOpen ? "conversation-download-menu" : undefined}
                    aria-expanded={downloadMenuOpen}
                    aria-haspopup="menu"
                    onClick={() => setDownloadMenuOpen((open) => !open)}
                    ref={downloadMenuTrigger}
                    role="menuitem"
                    type="button"
                  >Download<span aria-hidden="true" className="conversation-menu-chevron">›</span></button>
                  {downloadMenuOpen && <div
                    aria-label="Download"
                    className="conversation-download-menu"
                    id="conversation-download-menu"
                    role="menu"
                  >
                    {(["recording", "transcript", "notes", "summary"] as const).map((artifact) => {
                      const labels: Record<DownloadArtifact, string> = {
                        recording: "Recording",
                        transcript: "Transcript",
                        notes: "Notes",
                        summary: "Summary",
                      };
                      const available = artifact === "recording"
                        ? canDownloadRecording
                        : artifact === "transcript"
                          ? canDownloadTranscript
                          : artifact === "notes"
                            ? canDownloadNotes
                            : canDownloadSummary;
                      const label = labels[artifact];
                      const state = artifactDownloadStates[artifact];
                      const unavailableTitle = artifact === "recording"
                        ? "No recording is available to download."
                        : artifact === "transcript"
                          ? "No transcript is available to download."
                          : artifact === "notes"
                            ? notesError ?? "No notes are available to download."
                            : "No summary is available to download.";
                      return <button
                        aria-describedby={available ? undefined : `download-${artifact}-unavailable`}
                        aria-label={available ? undefined : `Download ${label}`}
                        className={available ? undefined : "conversation-download-unavailable"}
                        disabled={!available || state === "downloading"}
                        key={artifact}
                        onClick={() => void downloadArtifact(artifact)}
                        role="menuitem"
                        title={available ? undefined : unavailableTitle}
                        type="button"
                      >{state === "downloading"
                        ? `Downloading ${label.toLowerCase()}…`
                        : state === "initiated"
                          ? "Recording download started"
                          : state === "downloaded"
                            ? `${label} downloaded`
                            : state === "failed"
                              ? `Retry download ${label.toLowerCase()}`
                              : `Download ${label.toLowerCase()}`}
                        {!available && <span id={`download-${artifact}-unavailable`}>{unavailableTitle}</span>}
                      </button>;
                    })}
                  </div>}
                </div>}
              </div>}
            </div>}
          </div>
          <time>{new Date(conversation.occurred_at).toLocaleString()}</time>
          {speakerMapping && speakerMapping.speakers.length > 0 && <ul aria-description="Scrollable list of all speakers." aria-label="Conversation speakers" className="speaker-name-pills" tabIndex={0}>
            {speakerMapping.speakers.map((speaker) => {
              const displayName = speaker.display_name || speaker.label;
              return <li key={speaker.speaker_number} title={displayName}>{displayName}</li>;
            })}
          </ul>}
          {conversation.description && <p className="detail-description">{conversation.description}</p>}
          {(conversation.people.length > 0 || conversation.tags.length > 0) && <div className="detail-identities">
            {conversation.people.length > 0 && <div className="people-list" aria-label="People">
              {conversation.people.map((person, index) => <span className="person-chip" key={`${person}-${index}`} title={person}>
                <span className="person-avatar" aria-hidden="true">{initials(person)}</span>
                <span>{person}</span>
              </span>)}
            </div>}
            {conversation.tags.length > 0 && <div className="tag-list" aria-label="Tags">
              {conversation.tags.map((tag, index) => <span className="tag-pill" key={`${tag}-${index}`}>{tag}</span>)}
            </div>}
          </div>}
        </div>
      </div>
    </div>
    {error && <p role="alert" className="error conversation-action-error">{error}</p>}
    {artifactDownloadError && <p role="alert" className="error conversation-action-error">{artifactDownloadError}</p>}
    {stagingError && <p role="alert" className="error conversation-action-error">{stagingError}</p>}
    {summaryStageError && <p role="alert" className="error conversation-action-error">{summaryStageError}</p>}
    {speakerMappingError && <p role="alert" className="error conversation-action-error">{speakerMappingError}</p>}
    {sourceState === "ready" && playbackSource
      ? <RecordingPlayer
        key={`${playbackSource.kind}:${playbackSource.url}`}
        source={playbackSource}
        notes={notes}
        onAddNote={can("notes:human:create") ? addNote : undefined}
        onPlaybackTime={setPlaybackTime}
        onPlaybackFailureChange={setPlaybackFailed}
        initialTime={playbackTime}
        onSeekNote={seekToNote}
        seekRequest={noteSeekRequest}
        transcriptSeekRequest={transcriptSeekRequest}
        onTranscriptSeekHandled={markTranscriptSeekHandled}
        onNoteMarkerClick={openTimelineNote}
        onStreamError={refreshPlaybackGrant}
        onStreamUsable={() => {
          markStreamUsable();
          onPlaybackHandoffComplete();
        }}
        onSeekRequestHandled={markNoteSeekHandled}
        resumePlaybackOnLoad={resumePlaybackOnLoad || resumePlaybackAfterGrantRefresh.current}
      />
      : <div className="player-unavailable" role={sourceState === "failed" ? "alert" : "status"}>
        {sourceError ?? "Preparing recording playback…"}
        {sourceState === "failed" && audioSource && <div className="playback-source-actions">
          <button className="secondary" onClick={() => recordingGrant ? retryPlaybackGrant() : void retryInitialPlaybackGrant()} type="button">Retry playback</button>
          {recordingGrant && <button className="secondary" disabled={downloadState === "downloading" || downloadState === "downloaded"} onClick={() => void downloadForPlayback(recordingGrant)}>
            {downloadState === "downloading" ? "Downloading…" : downloadState === "downloaded" ? "Downloaded locally" : "Download for local playback"}
          </button>}
        </div>}
      </div>}
    <div className="review-workspace">
      <nav aria-label="Conversation content" className="review-tabs" role="tablist">
        {(["summary", "transcript", "notes", "status"] as const).map((tab) => <button
          aria-controls="review-panel"
          aria-selected={activeTab === tab}
          className={activeTab === tab ? "review-tab active" : "review-tab"}
          id={`review-tab-${tab}`}
          key={tab}
          onClick={() => setActiveTab(tab)}
          onKeyDown={(event) => handleReviewTabKeyDown(event, tab)}
          role="tab"
          tabIndex={activeTab === tab ? 0 : -1}
          type="button"
        >{tab[0].toUpperCase() + tab.slice(1)}</button>)}
      </nav>
      <div
        aria-labelledby={`review-tab-${activeTab}`}
        className="review-panel"
        id="review-panel"
        ref={reviewPanel}
        role="tabpanel"
        tabIndex={0}
      >
        {activeTab === "status" && <section className="status-tab-panel" aria-label="Processing status">
          <div className="status-rows">
            <StatusRow
              label="Recording"
              status={recordingStatus}
              issue={recordingIssue}
            />
            <StatusRow
              label="Transcription"
              status={transcriptionStatus}
              issue={transcriptIssue}
              progressLabel={transcriptionStatus === "RUNNING" || transcriptionStatus === "STAGED" ? "Transcription progress" : undefined}
            />
            <StatusRow
              label="Summary"
              status={summaryStatus}
              issue={summaryIssue}
              progressLabel={summaryStatus === "RUNNING" || summaryStatus === "STAGED" ? "Summary progress" : undefined}
            />
          </div>
          {transcriptionRetryAvailable && <button
            className="secondary"
            disabled={staging || transcriptionJobPending}
            onClick={() => void stageTranscription(true)}
            type="button"
          >{staging ? "Queueing transcription…" : "Retry transcription"}</button>}
        </section>}
        {activeTab === "notes" && <NotesList
          notes={notes}
          error={notesError}
          canDelete={can("notes:human:delete")}
          onDelete={(id) => void deleteNote(id)}
          onOpenNote={openNoteDetails}
          onSeekNote={seekToNote}
          currentTime={playbackTime}
        />}
        {activeTab === "transcript" && (conversation.transcript_available
          ? <>
            {transcriptionJobPending && transcriptJob && <JobProgress job={transcriptJob} />}
            {transcript
              ? <TranscriptView
                transcript={transcript}
                speakerMapping={speakerMapping}
                speakerMappingLoading={speakerMappingLoading}
                canRenameSpeakers={canWriteMetadata && !transcriptionJobPending && !staging}
                renameSaving={speakerRenameSaving}
                onRenameSpeaker={renameSpeaker}
                onSeekToSegment={seekToTranscriptSegment}
                playbackUnavailableMessage={transcriptPlaybackUnavailableMessage}
                currentTime={playbackTime}
              />
              : transcriptError
                ? <p role="alert" className="error">{transcriptError}</p>
                : <p role="status" className="transcript-loading">Loading transcript…</p>}
          </>
          : <section className="transcript-waiting" aria-label="Transcript status">
            <JobStatus
              job={transcriptJob}
              label="Transcription"
              status={transcriptJob ? jobStateLabel(transcriptJob.state) : jobsError ? "UNAVAILABLE" : jobsLoaded ? "READY TO START" : "CHECKING"}
            />
          </section>)}
        {activeTab === "summary" && (summaryJob?.state === "failed" || summaryJob?.state === "cancelled") && summaryIssue && <p role="alert" className="error">{summaryIssue}</p>}
        {activeTab === "summary" && (summaryAvailable
          ? <>
            {summaryJobPending && summaryJob && <JobProgress job={summaryJob} />}
            {summaryError && <p role="alert" className="error">{summaryError}</p>}
            {summary ? <SummaryView summary={summary} /> : !summaryError && <p role="status" className="transcript-loading">Loading summary…</p>}
          </>
          : <section className="summary-waiting" aria-label="Summary status">
            <JobStatus
              job={summaryJob}
              label="Summary"
              status={summaryJob ? jobStateLabel(summaryJob.state) : jobsError ? "UNAVAILABLE" : jobsLoaded ? "READY TO START" : "CHECKING"}
            />
          </section>)}
      </div>
    </div>
    <dialog className="metadata-dialog" onClose={() => setEditError(null)} ref={dialog}>
      <form className="metadata-form" onSubmit={(event) => void saveMetadata(event)}>
        <div className="dialog-heading"><p className="eyebrow">Conversation details</p><h2>Edit metadata</h2></div>
        <label>Title<input onChange={(event) => setDraft({ ...draft, name: event.currentTarget.value })} required value={draft.name} /></label>
        <label>Description<textarea onChange={(event) => setDraft({ ...draft, description: event.currentTarget.value || null })} value={draft.description ?? ""} /></label>
        <label>People<input onChange={(event) => setDraft({ ...draft, people: event.currentTarget.value.split(",").map((value) => value.trim()).filter(Boolean) })} value={draft.people.join(", ")} /></label>
        <label>Tags<input onChange={(event) => setDraft({ ...draft, tags: event.currentTarget.value.split(",").map((value) => value.trim()).filter(Boolean) })} value={draft.tags.join(", ")} /></label>
        {editError && <p role="alert" className="error">{editError}</p>}
        <div className="dialog-actions"><button className="secondary" onClick={() => dialog.current?.close()} type="button">Cancel</button><button disabled={saving} type="submit">{saving ? "Saving…" : "Save changes"}</button></div>
      </form>
    </dialog>
    <dialog
      className="note-dialog"
      onClose={() => { setSelectedNote(null); setNoteEditError(null); }}
      ref={noteDialog}
    >
      <form className="note-edit-form" onSubmit={(event) => void saveEditedNote(event)}>
        <div className="dialog-heading"><p className="eyebrow">Timeline note</p><h2>Note details</h2></div>
        {selectedNote && <time className="note-detail-timestamp">{formatTime(selectedNote.offset_seconds)} in recording</time>}
        <label htmlFor="note-detail-text">Note text<textarea
          autoFocus
          id="note-detail-text"
          onChange={(event) => setNoteDraft(event.currentTarget.value)}
          readOnly={!canEditSelectedNote}
          value={noteDraft}
        /></label>
        {!canEditSelectedNote && <p className="note-readonly">This note is read-only.</p>}
        {noteEditError && <p role="alert" className="error">{noteEditError}</p>}
        <div className="dialog-actions">
          <button className="secondary" onClick={() => noteDialog.current?.close()} type="button">Close</button>
          {canEditSelectedNote && <button disabled={noteSaving || !noteDraft.trim()} type="submit">{noteSaving ? "Saving…" : "Save note"}</button>}
        </div>
      </form>
    </dialog>
  </section>;
}

function NewRecordingRoute({
  capture,
  recordingConversation,
  previewUrl,
  uploadStatus,
  uploadError,
  onRetry,
  onDiscard,
  onPlaybackTime,
  onPlaybackStateChange,
  onBack,
}: {
  capture: ReturnType<typeof useAudioCapture>;
  recordingConversation: Conversation | null;
  previewUrl: string | null;
  uploadStatus: UploadStatus | null;
  uploadError: string | null;
  onRetry: () => void;
  onDiscard: () => void;
  onPlaybackTime: (conversationId: string, seconds: number) => void;
  onPlaybackStateChange: (conversationId: string, playing: boolean) => void;
  onBack: () => void;
}) {
  const pendingRecording = capture.pending;
  if (pendingRecording) {
    if (uploadStatus === "uploaded") return <Navigate to={`/recordings/${pendingRecording.conversationId}`} replace />;
    return <RecordingDetail
      recording={pendingRecording}
      conversation={recordingConversation}
      previewUrl={previewUrl}
      status={uploadStatus ?? "creating"}
      error={uploadError}
      onRetry={onRetry}
      onDiscard={onDiscard}
      onPlaybackTime={(time) => onPlaybackTime(pendingRecording.conversationId, time)}
      onPlaybackStateChange={(playing) => onPlaybackStateChange(pendingRecording.conversationId, playing)}
    />;
  }

  if (capture.state === "recording" || capture.error) return <RecordingScreen capture={capture} onBack={onBack} />;
  return <section className="detail-page">
    <button className="back-link" onClick={onBack}>← Recordings</button>
    <div className="player-unavailable">The recording is held only in memory and was lost when this page was refreshed.</div>
  </section>;
}

function ConversationRoute({
  api,
  session,
  conversations,
  recordingConversation,
  capture,
  previewUrl,
  uploadStatus,
  uploadError,
  playbackSnapshot,
  onRetry,
  onDiscard,
  onPlaybackTime,
  onPlaybackStateChange,
  onPlaybackHandoffComplete,
  onChanged,
  onLoaded,
  onRouteLoadState,
  onDeleted,
}: {
  api: ApiClient;
  session: AuthSession;
  conversations: Conversation[];
  recordingConversation: Conversation | null;
  capture: ReturnType<typeof useAudioCapture>;
  previewUrl: string | null;
  uploadStatus: UploadStatus | null;
  uploadError: string | null;
  playbackSnapshot: { conversationId: string; time: number; playing: boolean } | null;
  onRetry: () => void;
  onDiscard: () => void;
  onPlaybackTime: (conversationId: string, seconds: number) => void;
  onPlaybackStateChange: (conversationId: string, playing: boolean) => void;
  onPlaybackHandoffComplete: (conversationId: string) => void;
  onChanged: (item: Conversation) => void;
  onLoaded: (item: Conversation) => void;
  onRouteLoadState: (conversationId: string, state: "loading" | "failed" | "ready") => void;
  onDeleted: (conversationId: string) => void;
}) {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const pendingRecording = capture.pending;
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!conversationId) return;
    let active = true;
    const cached = recordingConversation?.id === conversationId
      ? recordingConversation
      : conversations.find((item) => item.id === conversationId);
    if (cached) {
      setConversation(cached);
      setError(null);
      setLoading(false);
      onRouteLoadState(conversationId, "ready");
      return () => { active = false; };
    }
    setLoading(true);
    onRouteLoadState(conversationId, "loading");
    void api.getConversation(conversationId).then((item) => {
      if (!active) return;
      setConversation(item);
      setError(null);
      setLoading(false);
      onLoaded(item);
      onRouteLoadState(conversationId, "ready");
    }).catch(() => {
      if (!active) return;
      setError("This recording could not be loaded.");
      setLoading(false);
      onRouteLoadState(conversationId, "failed");
    });
    return () => { active = false; };
  }, [api, conversationId, conversations, onLoaded, onRouteLoadState, recordingConversation]);

  if (pendingRecording && pendingRecording.conversationId === conversationId && uploadStatus && uploadStatus !== "uploaded") {
    return <RecordingDetail
      recording={pendingRecording}
      conversation={recordingConversation}
      previewUrl={previewUrl}
      status={uploadStatus}
      error={uploadError}
      onRetry={onRetry}
      onDiscard={onDiscard}
      onPlaybackTime={(time) => onPlaybackTime(pendingRecording.conversationId, time)}
      onPlaybackStateChange={(playing) => onPlaybackStateChange(pendingRecording.conversationId, playing)}
    />;
  }
  if (loading || !conversation || conversation.id !== conversationId) {
    return <section className="detail-page"><p role={error ? "alert" : "status"}>{error ?? "Loading recording…"}</p><button className="back-link" onClick={() => navigate("/recordings")}>← Recordings</button></section>;
  }
  const recordingPreview = pendingRecording?.conversationId === conversation.id && uploadStatus !== "uploaded" ? previewUrl : null;
  const audioSource = recordingPreview ?? (conversation.recording_available ? api.recordingUrl(conversation.id) : null);
  const conversationPlayback = playbackSnapshot?.conversationId === conversation.id ? playbackSnapshot : null;
  return <Review
    key={conversation.id}
    api={api}
    conversation={conversation}
    session={session}
    audioSource={audioSource}
    uploadStatus={pendingRecording?.conversationId === conversation.id ? uploadStatus : null}
    initialPlaybackTime={conversationPlayback?.time ?? 0}
    resumePlaybackOnLoad={conversationPlayback?.playing ?? false}
    onPlaybackHandoffComplete={() => onPlaybackHandoffComplete(conversation.id)}
    onChanged={(item) => { setConversation(item); onChanged(item); }}
    onDeleted={() => { onDeleted(conversation.id); navigate("/recordings", { replace: true }); }}
  />;
}

export function App({ session }: { session: AuthSession }) {
  const api = useMemo(() => new ApiClient(session), [session]);
  const capture = useAudioCapture();
  const location = useLocation();
  const detailId = matchPath({ path: "/recordings/:conversationId", end: true }, location.pathname)?.params.conversationId;
  const navigate = useNavigate();
  const navigateRef = useRef(navigate);
  const uploadStartedFor = useRef<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [recordingConversation, setRecordingConversation] = useState<Conversation | null>(null);
  const [routeLoadState, setRouteLoadState] = useState<{ conversationId: string; state: "loading" | "failed" | "ready" } | null>(null);
  useLayoutEffect(() => {
    if (detailId && detailId !== "new") setRouteLoadState({ conversationId: detailId, state: "loading" });
  }, [detailId, location.pathname]);
  const [uploadStatus, setUploadStatus] = useState<UploadStatus | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewPlayback, setPreviewPlayback] = useState<{ conversationId: string; time: number; playing: boolean } | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [importJob, setImportJob] = useState<ImportJob | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [maintenanceVisible, setMaintenanceVisible] = useState(false);
  const failedHealthChecks = useRef(0);
  const activeHealthCheck = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    activeHealthCheck.current?.abort();
    const controller = new AbortController();
    activeHealthCheck.current = controller;
    setListLoading(true);
    const requestTimeout = window.setTimeout(() => controller.abort(), 10000);
    try {
      setConversations(await api.listConversations(controller.signal));
      failedHealthChecks.current = 0;
      setListError(null);
      setMaintenanceVisible(false);
    } catch (error) {
      if (controller.signal.aborted || activeHealthCheck.current !== controller) return;
      if (error instanceof ApiError && error.status < 500) {
        failedHealthChecks.current = 0;
        setMaintenanceVisible(false);
        setListError("Recordings could not be loaded.");
      } else {
        setListError("The recording service is temporarily unavailable.");
        failedHealthChecks.current += 1;
        if (failedHealthChecks.current >= 3) setMaintenanceVisible(true);
      }
    } finally {
      if (activeHealthCheck.current === controller) {
        activeHealthCheck.current = null;
        setListLoading(false);
      }
      window.clearTimeout(requestTimeout);
    }
  }, [api]);

  useEffect(() => {
    void load();
    const retryTimer = window.setInterval(() => { void load(); }, 5000);
    return () => {
      window.clearInterval(retryTimer);
      activeHealthCheck.current?.abort();
      activeHealthCheck.current = null;
    };
  }, [load]);
  useEffect(() => { navigateRef.current = navigate; }, [navigate]);

  useEffect(() => {
    const recording = capture.pending?.blob;
    if (!recording || uploadStatus === "uploaded") {
      setPreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(recording);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [capture.pending?.blob, uploadStatus]);

  const uploadRecording = useCallback(async (recording: PendingRecording) => {
    setUploadStatus("creating");
    setUploadError(null);
    try {
      const created = await api.createConversation(recording.conversationId, recording.occurredAt);
      setRecordingConversation(created);
      setConversations((items) => [created, ...items.filter((item) => item.id !== created.id)]);
      navigateRef.current(`/recordings/${recording.conversationId}`, { replace: true });
      setUploadStatus("uploading");
      await api.uploadRecording(recording.conversationId, recording.blob);
      const items = await api.listConversations();
      const uploaded = items.find((item) => item.id === recording.conversationId) ?? { ...created, recording_available: true, sync_state: "processing" };
      setConversations(items);
      setRecordingConversation(uploaded);
      setUploadStatus("uploaded");
      capture.discard();
    } catch (error) {
      setUploadStatus("failed");
      setUploadError(messageFor(error));
    }
  }, [api, capture.discard]);

  const uploadImportedRecording = useCallback(async (job: ImportJob) => {
    const updateJob = (status: UploadStatus, error: string | null = null) => {
      setImportJob((current) => current?.conversationId === job.conversationId ? { ...current, status, error } : current);
    };
    updateJob("creating");
    try {
      const created = await api.createConversation(job.conversationId, job.occurredAt, recordingTitle(job.file.name));
      setConversations((items) => [created, ...items.filter((item) => item.id !== created.id)]);
      updateJob("uploading");
      await api.uploadRecording(job.conversationId, job.file, job.file.name);
      const items = await api.listConversations();
      setConversations(items);
      setListError(null);
      setImportJob((current) => current?.conversationId === job.conversationId ? null : current);
      navigateRef.current(`/recordings/${job.conversationId}`, { replace: true });
    } catch (error) {
      updateJob("failed", messageFor(error));
    }
  }, [api]);

  const importRecording = useCallback((file: File) => {
    if (!recordingMediaType(file.name, file.type)) {
      setListError("Choose a WAV, MP4, AAC, WebM, OGG, or MP3 audio file.");
      return;
    }
    const job: ImportJob = {
      file,
      conversationId: crypto.randomUUID(),
      occurredAt: new Date().toISOString(),
      status: "creating",
      error: null,
    };
    setListError(null);
    setImportJob(job);
    void uploadImportedRecording(job);
  }, [uploadImportedRecording]);

  const retryImport = useCallback(() => {
    if (importJob) void uploadImportedRecording(importJob);
  }, [importJob, uploadImportedRecording]);

  useEffect(() => {
    const recording = capture.pending;
    if (!recording) {
      uploadStartedFor.current = null;
      return;
    }
    if (uploadStartedFor.current === recording.conversationId) return;
    uploadStartedFor.current = recording.conversationId;
    void uploadRecording(recording);
  }, [capture.pending, uploadRecording]);

  const retryUpload = useCallback(() => {
    if (capture.pending) void uploadRecording(capture.pending);
  }, [capture.pending, uploadRecording]);

  const discardFailedRecording = useCallback(async () => {
    if (!capture.pending || uploadStatus !== "failed") return;
    if (recordingConversation) {
      try {
        await api.deleteConversation(recordingConversation.id);
        await load();
      } catch {
        setUploadError("The conversation could not be discarded.");
        return;
      }
    }
    capture.discard();
    setRecordingConversation(null);
    setUploadStatus(null);
    setUploadError(null);
    navigate("/recordings", { replace: true });
  }, [api, capture, load, navigate, recordingConversation, uploadStatus]);

  function beginRecording() {
    if (capture.pending && uploadStatus !== "uploaded") return;
    if (capture.pending) {
      capture.discard();
      setRecordingConversation(null);
      setUploadStatus(null);
      setUploadError(null);
    }
    navigate("/recordings/new");
    void capture.start();
  }

  function updatePreviewPlaybackTime(conversationId: string, time: number) {
    setPreviewPlayback((current) => ({
      conversationId,
      time,
      playing: current?.conversationId === conversationId ? current.playing : false,
    }));
  }

  function updatePreviewPlaybackState(conversationId: string, playing: boolean) {
    setPreviewPlayback((current) => ({
      conversationId,
      time: current?.conversationId === conversationId ? current.time : 0,
      playing,
    }));
  }

  function clearPreviewPlayback(conversationId: string) {
    setPreviewPlayback((current) => current?.conversationId === conversationId ? null : current);
  }

  const updateConversation = useCallback((updated: Conversation) => {
    setConversations((items) => items.some((item) => item.id === updated.id)
      ? items.map((item) => item.id === updated.id ? updated : item)
      : [updated, ...items]);
    setRecordingConversation((current) => current?.id === updated.id ? updated : current);
  }, []);
  const updateRouteLoadState = useCallback((conversationId: string, state: "loading" | "failed" | "ready") => {
    setRouteLoadState({ conversationId, state });
  }, []);

  function deleteConversation(conversationId: string) {
    setConversations((items) => items.filter((item) => item.id !== conversationId));
    if (capture.pending?.conversationId === conversationId) {
      capture.discard();
      setRecordingConversation(null);
      setUploadStatus(null);
      setUploadError(null);
    }
    void load();
  }

  const retryUploadOnRoute = useCallback(() => retryUpload(), [retryUpload]);
  const showBreadcrumb = Boolean(detailId && detailId !== "new");
  const headerConversation = conversations.find((item) => item.id === detailId)
    ?? (recordingConversation?.id === detailId ? recordingConversation : null);
  const breadcrumbLabel = headerConversation?.name
    ?? (routeLoadState && routeLoadState.conversationId === detailId && routeLoadState.state === "failed" ? "Recording unavailable" : "Loading recording…");
  const appInert = maintenanceVisible;
  return <div className="app-shell">
    <div aria-hidden={appInert} className="app-content" inert={appInert}>
      <header className="app-header">
        <div className="app-title-bar">
          <Link className="brand" to="/recordings"><span className="brand-mark" aria-hidden="true">m</span><span>muninn</span></Link>
          {showBreadcrumb && <nav aria-label="Breadcrumb" className="breadcrumb-nav">
            <ol>
              <li><Link to="/recordings">Recordings</Link></li>
              <li aria-current="page" title={breadcrumbLabel}>{breadcrumbLabel}</li>
            </ol>
          </nav>}
        </div>
        <div className="identity"><span>Signed in as</span><strong>{session.displayName}</strong>{session.onLogout && <button className="sign-out" onClick={session.onLogout} type="button">Sign out</button>}</div>
      </header>
    <main className="app-main">
      <Routes>
        <Route path="/" element={<Navigate replace to="/recordings" />} />
        <Route path="/recordings" element={<RecordingLibrary
          conversations={conversations}
          error={listError}
          loading={listLoading}
          importJob={importJob}
          recordingLocked={(capture.pending !== null && uploadStatus !== "uploaded") || importJob !== null}
          onImport={importRecording}
          onRecord={beginRecording}
          onRetryImport={retryImport}
          onSelect={(item) => navigate(`/recordings/${item.id}`)}
        />} />
        <Route path="/recordings/new" element={<NewRecordingRoute
          capture={capture}
          recordingConversation={recordingConversation}
          previewUrl={previewUrl}
          uploadStatus={uploadStatus}
          uploadError={uploadError}
          onRetry={retryUploadOnRoute}
          onDiscard={discardFailedRecording}
          onPlaybackTime={updatePreviewPlaybackTime}
          onPlaybackStateChange={updatePreviewPlaybackState}
          onBack={() => navigate("/recordings")}
        />} />
        <Route path="/recordings/:conversationId" element={<ConversationRoute
          api={api}
          session={session}
          conversations={conversations}
          recordingConversation={recordingConversation}
          capture={capture}
          previewUrl={previewUrl}
          uploadStatus={uploadStatus}
          uploadError={uploadError}
          playbackSnapshot={previewPlayback}
          onRetry={retryUploadOnRoute}
          onDiscard={discardFailedRecording}
          onPlaybackTime={updatePreviewPlaybackTime}
          onPlaybackStateChange={updatePreviewPlaybackState}
          onPlaybackHandoffComplete={clearPreviewPlayback}
          onChanged={updateConversation}
          onLoaded={updateConversation}
          onRouteLoadState={updateRouteLoadState}
          onDeleted={deleteConversation}
        />} />
        <Route path="*" element={<Navigate replace to="/recordings" />} />
      </Routes>
      </main>
    </div>
    {maintenanceVisible && <MaintenancePage checking={false} />}
  </div>;
}
