package app.muninn.capture

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CaptureWidgetTest {
    @Test
    fun widgetRequiresAnIntentionalHoldBeforeAStartAction() {
        assertFalse(WidgetGestureGuard.accepts(599L))
        assertTrue(WidgetGestureGuard.accepts(600L))
    }
}
