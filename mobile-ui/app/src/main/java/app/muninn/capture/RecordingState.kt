package app.muninn.capture

/** The states exposed to the app, service, and widget. */
enum class CaptureStatus { READY, RECORDING, STOPPING }

data class CaptureState(
    val status: CaptureStatus = CaptureStatus.READY,
    val startedAtMillis: Long? = null,
    val deadlineAtMillis: Long? = null,
    val elapsedMillis: Long = 0,
    val warningPlayed: Boolean = false,
)

enum class StopReason { USER, DEADLINE, RECOVERABLE_FAULT, IRRECOVERABLE_FAULT }

/** A deterministic state model. Android scheduling and audio I/O remain outside this class. */
class RecordingStateMachine(
    private val maximumDurationMillis: Long = CaptureConstants.DEFAULT_DEADLINE_MILLIS,
    private val warningLeadMillis: Long = CaptureConstants.WARNING_LEAD_MILLIS,
) {
    var state: CaptureState = CaptureState()
        private set

    fun start(nowMillis: Long): Boolean {
        if (state.status != CaptureStatus.READY) return false
        state = CaptureState(CaptureStatus.RECORDING, nowMillis, nowMillis + maximumDurationMillis, 0)
        return true
    }

    fun requestStop(): Boolean {
        if (state.status != CaptureStatus.RECORDING) return false
        state = state.copy(status = CaptureStatus.STOPPING)
        return true
    }

    fun stop(reason: StopReason): Boolean = requestStop()

    fun tick(nowMillis: Long): TickResult {
        if (state.status != CaptureStatus.RECORDING) return TickResult.None
        val started = state.startedAtMillis ?: return TickResult.None
        val deadline = state.deadlineAtMillis ?: return TickResult.None
        val elapsed = (nowMillis - started).coerceAtLeast(0)
        state = state.copy(elapsedMillis = elapsed)
        if (nowMillis >= deadline) {
            state = state.copy(status = CaptureStatus.STOPPING)
            return TickResult.DeadlineReached
        }
        if (!state.warningPlayed && nowMillis >= deadline - warningLeadMillis) {
            state = state.copy(warningPlayed = true)
            return TickResult.WarningDue
        }
        return TickResult.Progress
    }

    fun extend(extensionMillis: Long): Boolean {
        if (state.status != CaptureStatus.RECORDING || extensionMillis <= 0) return false
        state = state.copy(deadlineAtMillis = (state.deadlineAtMillis ?: return false) + extensionMillis)
        return true
    }

    fun finalized() { state = CaptureState() }
}

sealed interface TickResult {
    data object None : TickResult
    data object Progress : TickResult
    data object WarningDue : TickResult
    data object DeadlineReached : TickResult
}

object CaptureConstants {
    const val DEFAULT_DEADLINE_MILLIS = 2 * 60 * 60 * 1000L
    const val WARNING_LEAD_MILLIS = 5 * 60 * 1000L
    const val EXTENSION_15_MINUTES = 15 * 60 * 1000L
    const val EXTENSION_30_MINUTES = 30 * 60 * 1000L
    const val EXTENSION_1_HOUR = 60 * 60 * 1000L
}
