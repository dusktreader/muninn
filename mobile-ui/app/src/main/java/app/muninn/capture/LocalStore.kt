package app.muninn.capture

import java.io.File

data class RecordingMetadata(val id: String, val startedAtMillis: Long, val durationMillis: Long, val status: String)
data class OutboxOperation(val recordingId: String, val acknowledgedBytes: Long, val digest: String)

/** Small Room-shaped boundary. A future Room DAO can implement this without changing capture code. */
interface LocalRecordingStore {
    fun saveMetadata(metadata: RecordingMetadata)
    fun enqueue(operation: OutboxOperation)
    fun metadata(id: String): RecordingMetadata?
    fun pending(): List<OutboxOperation>
    fun removeAudio(id: String)
}

class InMemoryRecordingStore : LocalRecordingStore {
    private val metadataRows = linkedMapOf<String, RecordingMetadata>()
    private val outboxRows = linkedMapOf<String, OutboxOperation>()
    override fun saveMetadata(metadata: RecordingMetadata) { metadataRows[metadata.id] = metadata }
    override fun enqueue(operation: OutboxOperation) { outboxRows[operation.recordingId] = operation }
    override fun metadata(id: String) = metadataRows[id]
    override fun pending() = outboxRows.values.toList()
    override fun removeAudio(id: String) = Unit
}

/** File boundary for encrypted segments and manifests. Encryption is supplied by the production store. */
interface SegmentStore {
    fun segment(recordingId: String, sequence: Int): File
    fun manifest(recordingId: String): File
}
