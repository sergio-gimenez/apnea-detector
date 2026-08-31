package com.sergiogimenez.nocturne

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.workDataOf
import java.time.Duration
import java.time.Instant
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { RecorderApp() }
    }
}

private val Night = Color(0xFF070B0E)
private val Panel = Color(0xFF10171C)
private val Line = Color(0xFF25323A)
private val Cyan = Color(0xFF58D6D0)
private val Amber = Color(0xFFF0AD4E)
private val Muted = Color(0xFF8B979F)

@Composable
private fun RecorderApp() {
    val context = androidx.compose.ui.platform.LocalContext.current
    val store = remember { SessionStore(context) }
    var sessions by remember { mutableStateOf(store.listSessions()) }
    var activeId by remember { mutableStateOf(store.activeSessionId) }
    var backendUrl by remember { mutableStateOf(store.backendUrl) }
    var token by remember { mutableStateOf(store.apiToken) }
    var message by remember { mutableStateOf("") }
    var pendingStart by remember { mutableStateOf(false) }
    var deletePrompt by remember { mutableStateOf<DeleteRequest?>(null) }
    var uploadProgress by remember { mutableStateOf<Map<String, Int>>(emptyMap()) }
    var clockTick by remember { mutableStateOf(Instant.now().epochSecond) }
    val scope = rememberCoroutineScope()

    fun refresh() {
        activeId = store.activeSessionId
        sessions = store.listSessions()
    }

    /**
     * Opens the deletion prompt, first confirming with the backend that it holds every
     * chunk for captures this build did not upload itself.
     */
    fun requestDelete(session: SessionManifest) {
        if (session.status == "uploaded") {
            deletePrompt = DeleteRequest(session, verified = true)
            return
        }
        scope.launch {
            message = "Checking the backend copy…"
            runCatching {
                require(backendUrl.isNotBlank()) { "Set backend URL" }
                store.backendUrl = backendUrl
                store.apiToken = token
                BackendClient(backendUrl, token, store).uploadedChunkCount(session.id)
            }.onSuccess { remote ->
                when {
                    remote == null -> {
                        message = ""
                        deletePrompt = DeleteRequest(
                            session,
                            verified = false,
                            reason = "The backend has no copy of this capture.",
                        )
                    }
                    remote < session.chunks.size -> {
                        message = ""
                        deletePrompt = DeleteRequest(
                            session,
                            verified = false,
                            reason = "The backend holds only $remote of ${session.chunks.size} chunks.",
                        )
                    }
                    else -> {
                        store.markUploaded(session.id)
                        refresh()
                        message = "Backend copy confirmed: $remote chunks."
                        store.load(session.id)?.let {
                            deletePrompt = DeleteRequest(it, verified = true)
                        }
                    }
                }
            }.onFailure { error ->
                message = ""
                deletePrompt = DeleteRequest(
                    session,
                    verified = false,
                    reason = "Could not reach the backend (${error.message ?: "unknown error"}).",
                )
            }
        }
    }

    fun startRecording() {
        store.backendUrl = backendUrl
        store.apiToken = token
        runCatching {
            val session = store.createSession()
            try {
                ContextCompat.startForegroundService(
                    context,
                    Intent(context, RecordingService::class.java)
                        .setAction(RecordingService.ACTION_START)
                        .putExtra(RecordingService.EXTRA_SESSION_ID, session.id),
                )
            } catch (error: Throwable) {
                store.fail(session.id, error.message ?: "Could not start recorder")
                throw error
            }
        }.onSuccess {
            message = "Recording started. Screen may be locked."
        }.onFailure { error ->
            message = error.message ?: "Could not start recorder"
        }
        refresh()
    }

    val permissions = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { _ ->
        val microphoneGranted =
            ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED
        if (pendingStart && microphoneGranted) startRecording()
        else if (pendingStart) message = "Microphone permission is required."
        pendingStart = false
    }

    DisposableEffect(Unit) {
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(receiverContext: Context?, intent: Intent?) {
                if (intent?.action == UploadWorker.ACTION_STATE) {
                    message = intent.getStringExtra(UploadWorker.EXTRA_MESSAGE) ?: "Upload state changed."
                    refresh()
                    val uploadedId = intent.getStringExtra(UploadWorker.EXTRA_SESSION_ID)
                    val percent = intent.getIntExtra(UploadWorker.EXTRA_PROGRESS, -1)
                    if (uploadedId != null && percent >= 0) {
                        uploadProgress = uploadProgress + (uploadedId to percent)
                    }
                    if (intent.getBooleanExtra(UploadWorker.EXTRA_UPLOADED, false) && uploadedId != null) {
                        uploadProgress = uploadProgress - uploadedId
                        store.load(uploadedId)?.takeUnless { it.audioDeleted }?.let {
                            deletePrompt = DeleteRequest(it, verified = true)
                        }
                    }
                    return
                }
                val wasStopping = message.startsWith("Stopping")
                refresh()
                if (wasStopping && activeId == null) {
                    val latest = sessions.firstOrNull()
                    message = latest?.error ?: "Capture stopped and saved."
                }
            }
        }
        ContextCompat.registerReceiver(
            context,
            receiver,
            IntentFilter().apply {
                addAction(RecordingService.ACTION_STATE)
                addAction(UploadWorker.ACTION_STATE)
            },
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        onDispose { context.unregisterReceiver(receiver) }
    }
    LaunchedEffect(Unit) { refresh() }
    LaunchedEffect(activeId) {
        while (activeId != null) {
            delay(1_000)
            clockTick = Instant.now().epochSecond
        }
    }

    MaterialTheme {
        Surface(color = Night, modifier = Modifier.fillMaxSize()) {
            LazyColumn(
                modifier = Modifier.fillMaxSize().padding(horizontal = 20.dp),
                verticalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                item {
                    Spacer(Modifier.height(28.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(Modifier.width(10.dp).height(10.dp).background(Cyan, CircleShape))
                        Spacer(Modifier.width(10.dp))
                        Text("NOCTURNE / CAPTURE", color = Cyan, fontFamily = FontFamily.Monospace, fontSize = 12.sp)
                    }
                    Spacer(Modifier.height(38.dp))
                    Text("Listen through\nthe night.", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 48.sp, lineHeight = 48.sp)
                    Text("Screening prototype · not a medical diagnosis", color = Amber, fontSize = 12.sp, modifier = Modifier.padding(top = 14.dp))
                }
                item {
                    CaptureCard(
                        active = activeId != null,
                        activeSession = sessions.firstOrNull { it.id == activeId },
                        clockTick = clockTick,
                        onStart = {
                            val requested = mutableListOf<String>()
                            if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                                requested += Manifest.permission.RECORD_AUDIO
                            }
                            if (
                                Build.VERSION.SDK_INT >= 33 &&
                                ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
                            ) {
                                requested += Manifest.permission.POST_NOTIFICATIONS
                            }
                            if (requested.isEmpty()) {
                                startRecording()
                            } else {
                                pendingStart = true
                                permissions.launch(requested.toTypedArray())
                            }
                        },
                        onStop = {
                            context.startService(Intent(context, RecordingService::class.java).setAction(RecordingService.ACTION_STOP))
                            message = "Stopping after current audio buffer…"
                        },
                    )
                }
                item {
                    ConfigCard(
                        backendUrl = backendUrl,
                        token = token,
                        onBackendChange = { backendUrl = it },
                        onTokenChange = { token = it },
                    )
                }
                if (message.isNotBlank()) item { Text(message, color = Cyan, fontSize = 13.sp) }
                item { Text("CAPTURES", color = Muted, fontFamily = FontFamily.Monospace, fontSize = 11.sp, modifier = Modifier.padding(top = 14.dp)) }
                items(sessions, key = { it.id }) { session ->
                    SessionRow(
                        session = session,
                        localBytes = if (session.audioDeleted) 0L else store.audioBytes(session.id),
                        progressPercent = uploadProgress[session.id],
                        onDelete = { requestDelete(session) },
                        onUpload = {
                        store.backendUrl = backendUrl
                        store.apiToken = token
                        runCatching {
                            require(session.status != "recording") { "Stop capture before upload" }
                            require(!session.audioDeleted) { "Local audio was deleted; nothing to upload" }
                            require(backendUrl.isNotBlank()) { "Set backend URL" }
                            store.backendUrl = backendUrl
                            store.apiToken = token
                            val request = OneTimeWorkRequestBuilder<UploadWorker>()
                                .setInputData(workDataOf(UploadWorker.KEY_SESSION_ID to session.id))
                                .setConstraints(
                                    Constraints.Builder()
                                        .setRequiredNetworkType(NetworkType.CONNECTED)
                                        .build(),
                                )
                                .build()
                            WorkManager.getInstance(context).enqueueUniqueWork(
                                "upload-${session.id}",
                                ExistingWorkPolicy.KEEP,
                                request,
                            )
                        }.onSuccess {
                            message = "Upload queued. You may lock the screen."
                        }.onFailure { error ->
                            message = error.message ?: "Could not queue upload"
                        }
                        },
                    )
                }
                item { Spacer(Modifier.height(40.dp)) }
            }

            deletePrompt?.let { request ->
                DeleteAudioDialog(
                    request = request,
                    bytes = store.audioBytes(request.session.id),
                    onDismiss = { deletePrompt = null },
                    onConfirm = {
                        runCatching { store.deleteAudio(request.session.id, force = !request.verified) }
                            .onSuccess { freed ->
                                message = "Deleted ${formatBytes(freed)} of local audio. Backend copy kept."
                            }
                            .onFailure { error ->
                                message = error.message ?: "Could not delete local audio"
                            }
                        deletePrompt = null
                        refresh()
                    },
                )
            }
        }
    }
}

/** A pending deletion, and whether the backend was confirmed to hold the audio. */
private data class DeleteRequest(
    val session: SessionManifest,
    val verified: Boolean,
    val reason: String? = null,
)

@Composable
private fun DeleteAudioDialog(
    request: DeleteRequest,
    bytes: Long,
    onDismiss: () -> Unit,
    onConfirm: () -> Unit,
) {
    val session = request.session
    val night = session.startedAtUtc.take(16).replace('T', ' ')
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Panel,
        title = {
            Text(
                if (request.verified) "Delete local audio?" else "This night is not backed up",
                color = if (request.verified) Color.White else Amber,
            )
        },
        text = {
            Text(
                if (request.verified) {
                    "The ${session.chunks.size} chunks of $night (${formatBytes(bytes)}) are confirmed " +
                        "on the backend. Deleting them here frees space on the phone and cannot be " +
                        "undone. Recording metadata stays."
                } else {
                    "${request.reason} Deleting now destroys the only copy of $night " +
                        "(${session.chunks.size} chunks, ${formatBytes(bytes)}) and the recording " +
                        "cannot be recovered. Upload it first unless you are sure you do not want it."
                },
                color = Muted,
                fontSize = 13.sp,
            )
        },
        confirmButton = {
            TextButton(onClick = onConfirm) {
                Text(if (request.verified) "DELETE" else "DELETE ANYWAY", color = Amber)
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("KEEP", color = Cyan) } },
    )
}

@Composable
private fun CaptureCard(
    active: Boolean,
    activeSession: SessionManifest?,
    clockTick: Long,
    onStart: () -> Unit,
    onStop: () -> Unit,
) {
    Column(Modifier.fillMaxWidth().background(Panel).border(1.dp, Line).padding(20.dp)) {
        Text(if (active) "MICROPHONE ACTIVE" else "READY FOR OVERNIGHT CAPTURE", color = if (active) Amber else Muted, fontFamily = FontFamily.Monospace, fontSize = 11.sp)
        Spacer(Modifier.height(12.dp))
        Text(
            if (active) {
                elapsed(activeSession?.startedAtUtc, clockTick)
            } else {
                "16 kHz · mono · PCM 16-bit"
            },
            color = Color.White,
            fontFamily = FontFamily.Monospace,
            fontSize = 24.sp,
        )
        Text(if (active) "Audio remains local until you upload." else "One-minute recoverable WAV chunks", color = Muted, fontSize = 13.sp, modifier = Modifier.padding(top = 6.dp, bottom = 18.dp))
        Button(
            onClick = if (active) onStop else onStart,
            colors = ButtonDefaults.buttonColors(containerColor = if (active) Color(0xFF5A2424) else Cyan, contentColor = if (active) Color.White else Night),
            modifier = Modifier.fillMaxWidth(),
        ) { Text(if (active) "STOP CAPTURE" else "START CAPTURE", fontWeight = FontWeight.Bold) }
    }
}

@Composable
private fun ConfigCard(
    backendUrl: String,
    token: String,
    onBackendChange: (String) -> Unit,
    onTokenChange: (String) -> Unit,
) {
    Column(Modifier.fillMaxWidth().background(Panel).border(1.dp, Line).padding(20.dp)) {
        Text("UPLOAD TARGET", color = Muted, fontFamily = FontFamily.Monospace, fontSize = 11.sp)
        OutlinedTextField(
            value = backendUrl,
            onValueChange = onBackendChange,
            label = { Text("Backend URL") },
            placeholder = { Text("https://sleep.sergiogimenez.com") },
            singleLine = true,
            colors = uploadFieldColors(),
            modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
        )
        OutlinedTextField(
            value = token,
            onValueChange = onTokenChange,
            label = { Text("Prototype API token") },
            visualTransformation = PasswordVisualTransformation(),
            singleLine = true,
            colors = uploadFieldColors(),
            modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
        )
    }
}

@Composable
private fun uploadFieldColors() = OutlinedTextFieldDefaults.colors(
    focusedTextColor = Color.White,
    unfocusedTextColor = Color.White,
    disabledTextColor = Muted,
    cursorColor = Cyan,
    focusedLabelColor = Cyan,
    unfocusedLabelColor = Muted,
    focusedPlaceholderColor = Muted,
    unfocusedPlaceholderColor = Muted,
    focusedBorderColor = Cyan,
    unfocusedBorderColor = Line,
)

@Composable
private fun SessionRow(
    session: SessionManifest,
    localBytes: Long,
    progressPercent: Int?,
    onUpload: () -> Unit,
    onDelete: () -> Unit,
) {
    Row(
        Modifier.fillMaxWidth().background(Panel).border(1.dp, Line).padding(16.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(session.startedAtUtc.take(16).replace('T', ' '), color = Color.White, fontWeight = FontWeight.Bold)
            Text("${session.chunks.size} chunks · ${formatSamples(session.totalSamples)} · ${session.status}", color = Muted, fontFamily = FontFamily.Monospace, fontSize = 11.sp)
            Text(
                if (session.audioDeleted) "local audio deleted" else "${formatBytes(localBytes)} on phone",
                color = Muted,
                fontFamily = FontFamily.Monospace,
                fontSize = 11.sp,
            )
            if (progressPercent != null) {
                LinearProgressIndicator(
                    progress = { progressPercent / 100f },
                    color = Cyan,
                    trackColor = Line,
                    modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                )
                Text(
                    "uploading · $progressPercent% · you can lock the screen",
                    color = Cyan,
                    fontFamily = FontFamily.Monospace,
                    fontSize = 11.sp,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
            session.error?.let { Text(it, color = Amber, fontSize = 11.sp) }
        }
        if (progressPercent != null) {
            // upload in flight: no actions until it settles
        } else if (session.status != "recording" && !session.audioDeleted) {
            if (session.status != "uploaded") {
                Button(onClick = onUpload, colors = ButtonDefaults.buttonColors(containerColor = Line)) { Text("UPLOAD") }
                Spacer(Modifier.width(8.dp))
            }
            Button(
                onClick = onDelete,
                colors = ButtonDefaults.buttonColors(containerColor = Line, contentColor = Amber),
            ) { Text("DELETE") }
        }
    }
}

private fun formatBytes(bytes: Long): String = when {
    bytes >= 1_000_000_000 -> "%.1f GB".format(bytes / 1_000_000_000.0)
    bytes >= 1_000_000 -> "%.0f MB".format(bytes / 1_000_000.0)
    else -> "%.0f kB".format(bytes / 1_000.0)
}

private fun elapsed(start: String?, nowEpochSecond: Long): String {
    if (start == null) return "00:00:00"
    val duration = runCatching {
        Duration.between(Instant.parse(start), Instant.ofEpochSecond(nowEpochSecond))
    }.getOrDefault(Duration.ZERO)
    val seconds = duration.seconds.coerceAtLeast(0)
    return "%02d:%02d:%02d".format(seconds / 3600, seconds % 3600 / 60, seconds % 60)
}

private fun formatSamples(samples: Long): String {
    val seconds = samples / SessionStore.SAMPLE_RATE
    return "%dh %02dm".format(seconds / 3600, seconds % 3600 / 60)
}
