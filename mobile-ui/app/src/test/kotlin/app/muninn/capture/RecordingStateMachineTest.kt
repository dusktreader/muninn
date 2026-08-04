package app.muninn.capture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RecordingStateMachineTest {
    @Test
    fun startsOnlyFromReadyAndReportsElapsedProgress() {
        val machine = RecordingStateMachine(maximumDurationMillis = 10_000, warningLeadMillis = 2_000)
        assertTrue(machine.start(100L))
        assertFalse(machine.start(200L))
        assertEquals(TickResult.Progress, machine.tick(1_100L))
        assertEquals(1_000L, machine.state.elapsedMillis)
        assertEquals(CaptureStatus.RECORDING, machine.state.status)
    }

    @Test
    fun warnsOnceBeforeDeadlineAndStopsAtDeadline() {
        val machine = RecordingStateMachine(maximumDurationMillis = 10_000, warningLeadMillis = 2_000)
        machine.start(0L)
        assertEquals(TickResult.WarningDue, machine.tick(8_000L))
        assertEquals(TickResult.Progress, machine.tick(8_500L))
        assertEquals(TickResult.DeadlineReached, machine.tick(10_000L))
        assertEquals(CaptureStatus.STOPPING, machine.state.status)
    }

    @Test
    fun extensionsMoveDeadlineAndCannotChangeReadyOrStopping() {
        val machine = RecordingStateMachine(maximumDurationMillis = 10_000)
        assertFalse(machine.extend(CaptureConstants.EXTENSION_15_MINUTES))
        machine.start(0L)
        assertTrue(machine.extend(CaptureConstants.EXTENSION_15_MINUTES))
        assertEquals(10_000L + CaptureConstants.EXTENSION_15_MINUTES, machine.state.deadlineAtMillis)
        assertTrue(machine.requestStop())
        assertFalse(machine.extend(CaptureConstants.EXTENSION_30_MINUTES))
    }

    @Test
    fun stopIsExplicitAndFinalizationReturnsToReady() {
        val machine = RecordingStateMachine()
        machine.start(0L)
        assertTrue(machine.stop(StopReason.USER))
        assertEquals(CaptureStatus.STOPPING, machine.state.status)
        machine.finalized()
        assertEquals(CaptureStatus.READY, machine.state.status)
    }
}
