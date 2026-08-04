package app.muninn.capture

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews

object WidgetGestureGuard {
    const val MINIMUM_HOLD_MILLIS = 600L
    fun accepts(pressDurationMillis: Long) = pressDurationMillis >= MINIMUM_HOLD_MILLIS
}

/** The widget opens the fullscreen screen. A separate long-press action is required to start capture. */
class CaptureWidget : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) {
        ids.forEach { id ->
            val intent = Intent(context, MainActivity::class.java)
            val pending = PendingIntent.getActivity(context, id, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            manager.updateAppWidget(id, RemoteViews(context.packageName, R.layout.capture_widget).apply {
                setOnClickPendingIntent(R.id.widget_record, pending)
            })
        }
    }
}
