package app.muninn.capture

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.media.MediaRecorder
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.app.NotificationCompat
import java.io.File

interface SoundHooks {
    fun playDeadlineWarning()
    fun playAutomaticStop()
}

object DeviceSoundHooks : SoundHooks {
    override fun playDeadlineWarning() = Unit
    override fun playAutomaticStop() = Unit
}

/** Foreground capture authority. It owns audio I/O and survives activity destruction. */
class RecordingService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private val machine = RecordingStateMachine()
    private var recorder: MediaRecorder? = null
    private var soundHooks: SoundHooks = DeviceSoundHooks

    private val ticker = object : Runnable {
        override fun run() {
            when (machine.tick(System.currentTimeMillis())) {
                TickResult.WarningDue -> soundHooks.playDeadlineWarning()
                TickResult.DeadlineReached -> {
                    soundHooks.playAutomaticStop()
                    finalizeRecording(StopReason.DEADLINE)
                }
                else -> publish()
            }
            if (machine.state.status == CaptureStatus.RECORDING) handler.postDelayed(this, TICK_MILLIS)
        }
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(NOTIFICATION_ID, notification("Ready to record"))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> startRecording()
            ACTION_STOP -> finalizeRecording(StopReason.USER)
            ACTION_EXTEND -> machine.extend(intent.getLongExtra(EXTRA_EXTENSION, 0L))
        }
        publish()
        return START_STICKY
    }

    private fun startRecording() {
        if (!machine.start(System.currentTimeMillis())) return
        val output = File(cacheDir, "recording-${machine.state.startedAtMillis}.m4a")
        recorder = MediaRecorder(this).apply {
            setAudioSource(MediaRecorder.AudioSource.MIC)
            setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            setOutputFile(output)
            prepare()
            start()
        }
        handler.post(ticker)
        updateNotification("Recording in progress")
        publish()
    }

    private fun finalizeRecording(reason: StopReason) {
        if (machine.state.status == CaptureStatus.READY) return
        machine.stop(reason)
        publish()
        recorder?.runCatching { stop() }
        recorder?.release()
        recorder = null
        machine.finalized()
        handler.removeCallbacks(ticker)
        updateNotification("Recording saved")
        publish()
    }

    private fun publish() { CaptureCoordinator.publish(machine.state) }

    private fun notification(text: String): Notification = NotificationCompat.Builder(this, CHANNEL_ID)
        .setSmallIcon(android.R.drawable.ic_btn_speak_now)
        .setContentTitle("Muninn capture")
        .setContentText(text)
        .setOngoing(true)
        .setCategory(NotificationCompat.CATEGORY_SERVICE)
        .build()

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(text))
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "Recording", NotificationManager.IMPORTANCE_LOW),
            )
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        const val ACTION_START = "app.muninn.capture.START"
        const val ACTION_STOP = "app.muninn.capture.STOP"
        const val ACTION_EXTEND = "app.muninn.capture.EXTEND"
        const val EXTRA_EXTENSION = "extensionMillis"
        private const val CHANNEL_ID = "recording"
        private const val NOTIFICATION_ID = 7
        private const val TICK_MILLIS = 1_000L
    }
}
