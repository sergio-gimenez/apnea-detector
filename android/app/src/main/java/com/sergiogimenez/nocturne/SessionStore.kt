package com.sergiogimenez.nocturne

import android.content.Context
import android.os.Build
import android.os.Environment
import android.util.AtomicFile
import java.io.File
import java.io.RandomAccessFile
import java.time.Instant
import java.util.UUID
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

class SessionStore(private val context: Context) {
    private val json = Json { prettyPrint = true; ignoreUnknownKeys = true }
    private val preferences = context.getSharedPreferences("nocturne", Context.MODE_PRIVATE)
    private val root = File(
        context.getExternalFilesDir(Environment.DIRECTORY_MUSIC) ?: context.filesDir,
        "sessions",
    ).apply { mkdirs() }

    var backendUrl: String
        get() = preferences.getString("backend_url", "") ?: ""
        set(value) = preferences.edit().putString("backend_url", value.trim().trimEnd('/')).apply()

    var apiToken: String
        get() = preferences.getString("api_token", "") ?: ""
        set(value) = preferences.edit().putString("api_token", value.trim()).apply()

    val activeSessionId: String?
        get() = preferences.getString("active_session_id", null)

    @Synchronized
    fun createSession(): SessionManifest {
        check(activeSessionId == null) { "A capture is already active" }
        check(root.usableSpace >= MINIMUM_FREE_BYTES) {
            "At least 1.2 GB free storage is required before an overnight capture"
        }
        val now = Instant.now()
        val manifest = SessionManifest(
            id = UUID.randomUUID().toString(),
            deviceId = "${Build.MANUFACTURER} ${Build.MODEL}",
            startedAtUtc = now.toString(),
            startedAtMonotonicNs = System.nanoTime(),
        )
        sessionDirectory(manifest.id).mkdirs()
        save(manifest)
        preferences.edit().putString("active_session_id", manifest.id).apply()
        return manifest
    }

    @Synchronized
    fun load(sessionId: String): SessionManifest? = runCatching {
        json.decodeFromString<SessionManifest>(manifestFile(sessionId).readText())
    }.getOrNull()

    @Synchronized
    fun save(manifest: SessionManifest) {
        val destination = manifestFile(manifest.id)
        destination.parentFile?.mkdirs()
        writeAtomic(destination, json.encodeToString(manifest))
    }

    @Synchronized
    fun markRecordingStarted(sessionId: String, utc: Instant, monotonicNs: Long) {
        val current = requireNotNull(load(sessionId))
        if (current.totalSamples == 0L && current.chunks.isEmpty()) {
            save(current.copy(startedAtUtc = utc.toString(), startedAtMonotonicNs = monotonicNs))
        }
    }

    @Synchronized
    fun createPendingChunk(sessionId: String, metadata: AudioChunkMetadata) {
        writeAtomic(
            pendingMetadataFile(sessionId, metadata.sequence),
            json.encodeToString(metadata),
        )
    }

    @Synchronized
    fun commitChunk(sessionId: String, metadata: AudioChunkMetadata) {
        val current = requireNotNull(load(sessionId))
        if (current.chunks.none { it.sequence == metadata.sequence }) {
            save(
                current.copy(
                    totalSamples = maxOf(current.totalSamples, metadata.sampleOffset + metadata.sampleCount),
                    chunks = (current.chunks + metadata).sortedBy { it.sequence },
                ),
            )
        }
        pendingMetadataFile(sessionId, metadata.sequence).delete()
    }

    @Synchronized
    fun complete(sessionId: String) {
        val current = requireNotNull(load(sessionId))
        save(current.copy(status = "complete", completedAtUtc = Instant.now().toString()))
        clearActive(sessionId)
    }

    @Synchronized
    fun interrupt(sessionId: String) {
        recoverPending(sessionId)
        val current = requireNotNull(load(sessionId))
        save(current.copy(status = "interrupted", completedAtUtc = Instant.now().toString()))
        clearActive(sessionId)
    }

    @Synchronized
    fun fail(sessionId: String, message: String) {
        load(sessionId)?.let { save(it.copy(status = "error", error = message)) }
        clearActive(sessionId)
    }

    @Synchronized
    fun recoverPending(sessionId: String) {
        val directory = sessionDirectory(sessionId)
        directory.listFiles { file -> file.name.endsWith(".pending.json") }?.forEach { pending ->
            val original = json.decodeFromString<AudioChunkMetadata>(pending.readText())
            val part = File(directory, "${original.fileName}.part")
            val final = File(directory, original.fileName)
            if (part.exists() && part.length() > WAV_HEADER_BYTES) {
                val samples = (part.length() - WAV_HEADER_BYTES) / 2
                repairWavHeader(part, samples, original.sampleRate)
                check(part.renameTo(final)) { "Could not recover ${part.name}" }
                commitChunk(sessionId, original.copy(sampleCount = samples))
            } else if (final.exists()) {
                val samples = (final.length() - WAV_HEADER_BYTES) / 2
                commitChunk(sessionId, original.copy(sampleCount = samples))
            } else {
                pending.delete()
            }
        }
    }

    fun listSessions(): List<SessionManifest> = root.listFiles()
        ?.filter { it.isDirectory }
        ?.mapNotNull { load(it.name) }
        ?.sortedByDescending { it.startedAtUtc }
        ?: emptyList()

    fun sessionDirectory(sessionId: String): File = File(root, sessionId)

    fun chunkFile(sessionId: String, fileName: String): File = File(sessionDirectory(sessionId), fileName)

    private fun manifestFile(sessionId: String) = File(sessionDirectory(sessionId), "metadata.json")

    private fun pendingMetadataFile(sessionId: String, sequence: Int) =
        File(sessionDirectory(sessionId), "audio_${sequence.toString().padStart(5, '0')}.pending.json")

    private fun clearActive(sessionId: String) {
        if (activeSessionId == sessionId) preferences.edit().remove("active_session_id").apply()
    }

    private fun writeAtomic(destination: File, content: String) {
        val atomicFile = AtomicFile(destination)
        val stream = atomicFile.startWrite()
        try {
            stream.write(content.toByteArray(Charsets.UTF_8))
            atomicFile.finishWrite(stream)
        } catch (error: Throwable) {
            atomicFile.failWrite(stream)
            throw error
        }
    }

    companion object {
        const val SAMPLE_RATE = 16_000
        const val CHUNK_SECONDS = 60
        const val WAV_HEADER_BYTES = 44L
        const val MINIMUM_FREE_BYTES = 1_200_000_000L

        fun repairWavHeader(file: File, sampleCount: Long, sampleRate: Int) {
            RandomAccessFile(file, "rw").use { wav ->
                val dataBytes = sampleCount * 2
                wav.seek(4)
                writeLittleEndianInt(wav, 36 + dataBytes)
                wav.seek(40)
                writeLittleEndianInt(wav, dataBytes)
                wav.fd.sync()
            }
        }

        fun writeLittleEndianShort(file: RandomAccessFile, value: Long) {
            file.write((value and 0xff).toInt())
            file.write((value shr 8 and 0xff).toInt())
        }

        fun writeLittleEndianInt(file: RandomAccessFile, value: Long) {
            file.write((value and 0xff).toInt())
            file.write((value shr 8 and 0xff).toInt())
            file.write((value shr 16 and 0xff).toInt())
            file.write((value shr 24 and 0xff).toInt())
        }
    }
}
