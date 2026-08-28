package com.sergiogimenez.nocturne

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTimestamp
import android.media.MediaRecorder
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import java.time.Instant
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

class RecordingService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val stopping = AtomicBoolean(false)
    private var recordingJob: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val store = SessionStore(this)
        if (intent?.action == ACTION_STOP) {
            stopping.set(true)
            if (recordingJob?.isActive != true) {
                store.activeSessionId?.let { sessionId ->
                    runCatching { store.interrupt(sessionId) }
                        .onFailure { store.fail(sessionId, it.message ?: "Recovery failed") }
                }
                sendState()
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelfResult(startId)
            }
            return START_NOT_STICKY
        }
        if (recordingJob?.isActive == true) return START_STICKY
        val sessionId = intent?.getStringExtra(EXTRA_SESSION_ID) ?: store.activeSessionId
        if (sessionId == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        startForeground(NOTIFICATION_ID, notification("Preparing microphone…"))
        recordingJob = scope.launch { record(sessionId, store) }
        return START_STICKY
    }

    private fun record(sessionId: String, store: SessionStore) {
        var recorder: AudioRecord? = null
        var writer: WavChunkWriter? = null
        var failureMessage: String? = null
        try {
            store.recoverPending(sessionId)
            val current = requireNotNull(store.load(sessionId)) { "Session metadata missing" }
            val source = chooseAudioSource()
            val minimumBytes = AudioRecord.getMinBufferSize(
                SessionStore.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
            )
            check(minimumBytes > 0) { "Unsupported 16 kHz mono recording format" }
            check(
                ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
                    PackageManager.PERMISSION_GRANTED,
            ) { "Microphone permission was revoked" }
            recorder = AudioRecord.Builder()
                .setAudioSource(source)
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(SessionStore.SAMPLE_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                        .build(),
                )
                .setBufferSizeInBytes(maxOf(minimumBytes * 2, SessionStore.SAMPLE_RATE * 2))
                .build()
            check(recorder.state == AudioRecord.STATE_INITIALIZED) { "Microphone initialization failed" }

            wakeLock = getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "$packageName:overnight-recording")
                .apply { acquire(24 * 60 * 60 * 1000L) }

            val callMonotonic = System.nanoTime()
            val callUtc = Instant.now()
            recorder.startRecording()
            check(recorder.recordingState == AudioRecord.RECORDSTATE_RECORDING) { "Microphone did not start" }
            updateNotification("Recording · keep phone charging")

            val buffer = ShortArray(maxOf(minimumBytes / 2, SessionStore.SAMPLE_RATE / 5))
            var manifest = current
            var totalSamples = manifest.totalSamples
            var sequence = (manifest.chunks.maxOfOrNull { it.sequence } ?: -1) + 1
            var segmentSamples = 0L
            var segmentStartMonotonic = callMonotonic
            var segmentStartUtc = callUtc
            var timestampResolved = false
            var metadata: AudioChunkMetadata? = null

            while (!stopping.get()) {
                val read = recorder.read(buffer, 0, buffer.size, AudioRecord.READ_BLOCKING)
                if (read < 0) error("Microphone read failed with code $read")
                if (read == 0) continue

                if (!timestampResolved) {
                    val timestamp = AudioTimestamp()
                    if (recorder.getTimestamp(timestamp, AudioTimestamp.TIMEBASE_MONOTONIC) == AudioRecord.SUCCESS) {
                        segmentStartMonotonic = timestamp.nanoTime -
                            timestamp.framePosition * 1_000_000_000L / SessionStore.SAMPLE_RATE
                        segmentStartUtc = callUtc.plusNanos(segmentStartMonotonic - callMonotonic)
                    }
                    store.markRecordingStarted(sessionId, segmentStartUtc, segmentStartMonotonic)
                    manifest = requireNotNull(store.load(sessionId))
                    totalSamples = manifest.totalSamples
                    timestampResolved = true
                }

                var offset = 0
                while (offset < read) {
                    if (writer == null) {
                        val fileName = "audio_${sequence.toString().padStart(5, '0')}.wav"
                        val chunkMonotonic = segmentStartMonotonic +
                            segmentSamples * 1_000_000_000L / SessionStore.SAMPLE_RATE
                        val chunkUtc = segmentStartUtc.plusNanos(
                            segmentSamples * 1_000_000_000L / SessionStore.SAMPLE_RATE,
                        )
                        metadata = AudioChunkMetadata(
                            sequence = sequence,
                            fileName = fileName,
                            sampleOffset = totalSamples,
                            sampleCount = 0,
                            startedAtUtc = chunkUtc.toString(),
                            startedAtMonotonicNs = chunkMonotonic,
                        )
                        store.createPendingChunk(sessionId, metadata)
                        writer = WavChunkWriter(store.sessionDirectory(sessionId), fileName, SessionStore.SAMPLE_RATE)
                    }
                    val remaining = SessionStore.CHUNK_SECONDS * SessionStore.SAMPLE_RATE - writer.sampleCount
                    val take = minOf((read - offset).toLong(), remaining).toInt()
                    writer.write(buffer, offset, take)
                    offset += take
                    segmentSamples += take
                    if (writer.sampleCount == SessionStore.CHUNK_SECONDS * SessionStore.SAMPLE_RATE.toLong()) {
                        val completed = requireNotNull(metadata).copy(sampleCount = writer.sampleCount)
                        writer.closeAndCommit()
                        store.commitChunk(sessionId, completed)
                        totalSamples += completed.sampleCount
                        sequence += 1
                        writer = null
                        metadata = null
                        sendState()
                    }
                }
            }

            writer?.let { activeWriter ->
                if (activeWriter.sampleCount > 0) {
                    val completed = requireNotNull(metadata).copy(sampleCount = activeWriter.sampleCount)
                    activeWriter.closeAndCommit()
                    store.commitChunk(sessionId, completed)
                } else {
                    activeWriter.closeAfterFailure()
                }
            }
            recorder.stop()
            store.complete(sessionId)
        } catch (error: Throwable) {
            writer?.closeAfterFailure()
            val message = error.message ?: error.javaClass.simpleName
            failureMessage = message
            store.fail(sessionId, message)
        } finally {
            runCatching { recorder?.release() }
            if (wakeLock?.isHeld == true) wakeLock?.release()
            wakeLock = null
            sendState()
            stopForeground(STOP_FOREGROUND_REMOVE)
            failureMessage?.let { message ->
                getSystemService(NotificationManager::class.java).notify(
                    NOTIFICATION_ID,
                    failureNotification(message),
                )
            }
            stopSelf()
        }
    }

    private fun chooseAudioSource(): Int {
        val manager = getSystemService(AudioManager::class.java)
        return if (manager.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED) == "true") {
            MediaRecorder.AudioSource.UNPROCESSED
        } else {
            MediaRecorder.AudioSource.VOICE_RECOGNITION
        }
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val stop = PendingIntent.getService(
            this,
            1,
            Intent(this, RecordingService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("Nocturne overnight capture")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(0, "Stop", stop)
            .build()
    }

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification(text))
    }

    private fun failureNotification(message: String): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_notify_error)
            .setContentTitle("Nocturne recording stopped")
            .setContentText(message)
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
    }

    private fun createNotificationChannel() {
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                "Overnight recording",
                NotificationManager.IMPORTANCE_LOW,
            ),
        )
    }

    private fun sendState() {
        sendBroadcast(Intent(ACTION_STATE).setPackage(packageName))
    }

    override fun onDestroy() {
        stopping.set(true)
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        const val ACTION_START = "com.sergiogimenez.nocturne.START"
        const val ACTION_STOP = "com.sergiogimenez.nocturne.STOP"
        const val ACTION_STATE = "com.sergiogimenez.nocturne.STATE"
        const val EXTRA_SESSION_ID = "session_id"
        private const val CHANNEL_ID = "overnight-recording"
        private const val NOTIFICATION_ID = 42
    }
}
