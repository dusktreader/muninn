package app.muninn.capture

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.View
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat

class MainActivity : ComponentActivity() {
    private val microphoneRequest = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) sendServiceIntent(RecordingService.ACTION_START)
    }

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
        setContent { CaptureScreen(::toggleRecording, ::finalizeRecording, ::extendRecording) }
    }

    private fun toggleRecording() {
        if (CaptureCoordinator.state.value.status == CaptureStatus.READY) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                sendServiceIntent(RecordingService.ACTION_START)
            } else microphoneRequest.launch(Manifest.permission.RECORD_AUDIO)
        } else sendServiceIntent(RecordingService.ACTION_STOP)
    }

    private fun finalizeRecording() {
        if (CaptureCoordinator.state.value.status != CaptureStatus.READY) sendServiceIntent(RecordingService.ACTION_STOP)
        finish()
    }

    private fun extendRecording(extensionMillis: Long) {
        sendServiceIntent(RecordingService.ACTION_EXTEND, extensionMillis)
    }

    private fun sendServiceIntent(action: String, extensionMillis: Long? = null) {
        val intent = Intent(this, RecordingService::class.java).setAction(action)
        extensionMillis?.let { intent.putExtra(RecordingService.EXTRA_EXTENSION, it) }
        if (action == RecordingService.ACTION_START) ContextCompat.startForegroundService(this, intent)
        else startService(intent)
    }
}

@androidx.compose.runtime.Composable
fun CaptureScreen(onRecord: () -> Unit, onClose: () -> Unit, onExtend: (Long) -> Unit) {
    val state by CaptureCoordinator.state.collectAsState()
    val recording = state.status != CaptureStatus.READY
    Surface(modifier = Modifier.fillMaxSize(), color = Color(0xFFF8F5EF)) {
      Column(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text("MUNINN CAPTURE", style = MaterialTheme.typography.labelLarge, color = Color(0xFF6B6258))
        Text(if (recording) formatElapsed(state.elapsedMillis) else "Ready", style = MaterialTheme.typography.displaySmall)
        if (recording) Text("Recording indicator active", modifier = Modifier.padding(8.dp), color = Color(0xFF9B2226))
        else Text("Ready for an intentional recording", modifier = Modifier.padding(8.dp), color = Color(0xFF6B6258))
        Button(
            onClick = onRecord,
            modifier = Modifier.size(240.dp),
            enabled = state.status != CaptureStatus.STOPPING,
            colors = androidx.compose.material3.ButtonDefaults.buttonColors(
                containerColor = if (recording) Color(0xFF8B0000) else Color(0xFFD32F2F),
            ),
        ) { Text(if (recording) "Stop recording" else "Record") }
        if (recording) Button(onClick = onClose, modifier = Modifier.padding(top = 16.dp)) { Text("Close and finalize") }
        if (recording) {
            androidx.compose.foundation.layout.Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                modifier = Modifier.padding(top = 8.dp),
            ) {
                Button(onClick = { onExtend(CaptureConstants.EXTENSION_15_MINUTES) }) { Text("+15m") }
                Button(onClick = { onExtend(CaptureConstants.EXTENSION_30_MINUTES) }) { Text("+30m") }
                Button(onClick = { onExtend(CaptureConstants.EXTENSION_1_HOUR) }) { Text("+1h") }
            }
        }
      }
    }
}

fun formatElapsed(millis: Long): String {
    val totalSeconds = millis / 1_000
    return "%02d:%02d:%02d".format(totalSeconds / 3_600, (totalSeconds % 3_600) / 60, totalSeconds % 60)
}
