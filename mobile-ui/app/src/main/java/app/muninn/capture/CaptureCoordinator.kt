package app.muninn.capture

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/** Process-local projection shared by the activity and widget; the service is the write authority. */
object CaptureCoordinator {
    private val mutableState = MutableStateFlow(CaptureState())
    val state: StateFlow<CaptureState> = mutableState
    fun publish(value: CaptureState) { mutableState.value = value }
}
