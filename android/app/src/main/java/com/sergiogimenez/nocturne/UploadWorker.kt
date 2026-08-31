package com.sergiogimenez.nocturne

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import java.io.IOException

class UploadWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        val sessionId = inputData.getString(KEY_SESSION_ID) ?: return Result.failure()
        val store = SessionStore(applicationContext)
        val backendUrl = store.backendUrl
        val token = store.apiToken
        if (backendUrl.isBlank()) return fail("Upload failed: backend URL is missing.")

        createNotificationChannels()
        setForeground(foregroundInfo("Preparing upload…"))
        return try {
            var lastPercent = -1
            val chunks = BackendClient(backendUrl, token, store).upload(sessionId) { done, total, label ->
                val percent = if (total > 0) done * 100 / total else 0
                // one update per whole percent: a long night is hundreds of chunks
                if (percent != lastPercent) {
                    lastPercent = percent
                    notificationManager.notify(
                        FOREGROUND_NOTIFICATION_ID,
                        progressNotification("$label · $percent%", done, total),
                    )
                    sendProgress(sessionId, percent, label)
                }
            }
            store.markUploaded(sessionId)
            val message = "$chunks chunks uploaded and analyzed."
            notificationManager.notify(COMPLETION_NOTIFICATION_ID, resultNotification(message, false))
            sendState(message, sessionId, uploaded = true)
            Result.success()
        } catch (error: Throwable) {
            val message = "Upload failed: ${error.message ?: error.javaClass.simpleName}"
            if (error is IOException && !message.contains("Backend 4") && runAttemptCount < MAX_RETRIES) {
                sendState("Upload interrupted; retrying automatically.")
                Result.retry()
            } else {
                fail(message)
            }
        }
    }

    private fun fail(message: String): Result {
        createNotificationChannels()
        notificationManager.notify(COMPLETION_NOTIFICATION_ID, resultNotification(message, true))
        sendState(message)
        return Result.failure()
    }

    private fun uploadedSessionId() = inputData.getString(KEY_SESSION_ID)

    private fun foregroundInfo(text: String): ForegroundInfo {
        val notification = progressNotification(text)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ForegroundInfo(
                FOREGROUND_NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            ForegroundInfo(FOREGROUND_NOTIFICATION_ID, notification)
        }
    }

    private fun progressNotification(
        text: String,
        done: Int = 0,
        total: Int = 0,
    ) = NotificationCompat.Builder(applicationContext, PROGRESS_CHANNEL_ID)
        .setSmallIcon(android.R.drawable.stat_sys_upload)
        .setContentTitle("Nocturne upload")
        .setContentText(text)
        .setContentIntent(openApp())
        .setOngoing(true)
        .setOnlyAlertOnce(true)
        .setProgress(total, done, total <= 0)
        .build()

    private fun resultNotification(text: String, failed: Boolean) = NotificationCompat.Builder(
        applicationContext,
        RESULT_CHANNEL_ID,
    )
        .setSmallIcon(
            if (failed) android.R.drawable.stat_notify_error
            else android.R.drawable.stat_sys_upload_done,
        )
        .setContentTitle(if (failed) "Nocturne upload failed" else "Nocturne upload complete")
        .setContentText(text)
        .setContentIntent(openApp())
        .setAutoCancel(true)
        .build()

    private fun openApp() = PendingIntent.getActivity(
        applicationContext,
        2,
        Intent(applicationContext, MainActivity::class.java),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    private fun createNotificationChannels() {
        notificationManager.createNotificationChannel(
            NotificationChannel(
                PROGRESS_CHANNEL_ID,
                "Background uploads",
                NotificationManager.IMPORTANCE_LOW,
            ),
        )
        notificationManager.createNotificationChannel(
            NotificationChannel(
                RESULT_CHANNEL_ID,
                "Upload results",
                NotificationManager.IMPORTANCE_DEFAULT,
            ),
        )
    }

    private fun sendProgress(sessionId: String, percent: Int, label: String) {
        applicationContext.sendBroadcast(
            Intent(ACTION_STATE)
                .setPackage(applicationContext.packageName)
                .putExtra(EXTRA_MESSAGE, "$label · $percent%")
                .putExtra(EXTRA_SESSION_ID, sessionId)
                .putExtra(EXTRA_PROGRESS, percent),
        )
    }

    private fun sendState(message: String, sessionId: String? = null, uploaded: Boolean = false) {
        applicationContext.sendBroadcast(
            Intent(ACTION_STATE)
                .setPackage(applicationContext.packageName)
                .putExtra(EXTRA_MESSAGE, message)
                .putExtra(EXTRA_SESSION_ID, sessionId ?: uploadedSessionId())
                .putExtra(EXTRA_UPLOADED, uploaded)
                .putExtra(EXTRA_PROGRESS, -1),
        )
    }

    private val notificationManager: NotificationManager
        get() = applicationContext.getSystemService(NotificationManager::class.java)

    companion object {
        const val KEY_SESSION_ID = "session_id"
        const val ACTION_STATE = "com.sergiogimenez.nocturne.UPLOAD_STATE"
        const val EXTRA_MESSAGE = "message"
        const val EXTRA_SESSION_ID = "uploaded_session_id"
        const val EXTRA_UPLOADED = "uploaded"
        const val EXTRA_PROGRESS = "progress"
        private const val PROGRESS_CHANNEL_ID = "background-uploads"
        private const val RESULT_CHANNEL_ID = "upload-results"
        private const val FOREGROUND_NOTIFICATION_ID = 84
        private const val COMPLETION_NOTIFICATION_ID = 85
        private const val MAX_RETRIES = 3
    }
}
