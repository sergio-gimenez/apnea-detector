package com.sergiogimenez.nocturne

import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody

class BackendClient(
    private val baseUrl: String,
    private val token: String,
    private val store: SessionStore,
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.MINUTES)
        .readTimeout(60, TimeUnit.MINUTES)
        .build()
    private val json = Json { ignoreUnknownKeys = true }
    private val jsonType = "application/json".toMediaType()

    suspend fun upload(sessionId: String, progress: (String) -> Unit): Int = withContext(Dispatchers.IO) {
        store.recoverPending(sessionId)
        val session = requireNotNull(store.load(sessionId)) { "Session metadata missing" }
        require(baseUrl.startsWith("https://")) {
            "Backend URL must use HTTPS"
        }
        progress("Creating remote session")
        postJson(
            "/api/sessions",
            json.encodeToString(
                CreateSessionRequest(
                    id = session.id,
                    deviceId = session.deviceId,
                    startedAtUtc = session.startedAtUtc,
                    startedAtMonotonicNs = session.startedAtMonotonicNs,
                    sampleRate = session.sampleRate,
                ),
            ),
        )
        session.chunks.forEachIndexed { index, chunk ->
            progress("Uploading chunk ${index + 1} / ${session.chunks.size}")
            val file = store.chunkFile(sessionId, chunk.fileName)
            check(file.exists()) { "Missing ${chunk.fileName}" }
            val body = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("sequence", chunk.sequence.toString())
                .addFormDataPart("sample_offset", chunk.sampleOffset.toString())
                .addFormDataPart("sample_count", chunk.sampleCount.toString())
                .addFormDataPart("started_at_utc", chunk.startedAtUtc)
                .addFormDataPart("started_at_monotonic_ns", chunk.startedAtMonotonicNs.toString())
                .addFormDataPart("file", chunk.fileName, file.asRequestBody("audio/wav".toMediaType()))
                .build()
            execute(Request.Builder().url("$baseUrl/api/sessions/$sessionId/audio-chunks").post(body))
        }
        progress("Running initial analysis")
        postJson("/api/sessions/$sessionId/complete", "")
        session.chunks.size
    }

    private fun postJson(path: String, body: String) {
        execute(
            Request.Builder()
                .url("$baseUrl$path")
                .post(body.toRequestBody(if (body.isEmpty()) null else jsonType)),
        )
    }

    private fun execute(builder: Request.Builder) {
        if (token.isNotBlank()) builder.header("Authorization", "Bearer $token")
        client.newCall(builder.build()).execute().use { response ->
            if (!response.isSuccessful) {
                throw IOException("Backend ${response.code}: ${response.body.string().take(300)}")
            }
        }
    }
}
