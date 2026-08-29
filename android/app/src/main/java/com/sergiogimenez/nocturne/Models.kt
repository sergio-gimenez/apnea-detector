package com.sergiogimenez.nocturne

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class AudioChunkMetadata(
    val sequence: Int,
    val fileName: String,
    val sampleOffset: Long,
    val sampleCount: Long,
    val startedAtUtc: String,
    val startedAtMonotonicNs: Long,
    val sampleRate: Int = SessionStore.SAMPLE_RATE,
)

@Serializable
data class SessionManifest(
    val id: String,
    val deviceId: String,
    val startedAtUtc: String,
    val startedAtMonotonicNs: Long,
    val sampleRate: Int = SessionStore.SAMPLE_RATE,
    val status: String = "recording",
    val totalSamples: Long = 0,
    val completedAtUtc: String? = null,
    val uploadedAtUtc: String? = null,
    val audioDeleted: Boolean = false,
    val error: String? = null,
    val chunks: List<AudioChunkMetadata> = emptyList(),
)

@Serializable
data class CreateSessionRequest(
    val id: String,
    @SerialName("device_id") val deviceId: String,
    @SerialName("started_at_utc") val startedAtUtc: String,
    @SerialName("started_at_monotonic_ns") val startedAtMonotonicNs: Long,
    @SerialName("sample_rate") val sampleRate: Int,
)
