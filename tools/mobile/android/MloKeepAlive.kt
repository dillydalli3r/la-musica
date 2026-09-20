// Android keep-alive for the embedded la musica backend.
//
// The backend on Android is not a child process: it is this app's own process,
// with CPython embedded in it (see desktop/src-tauri/src/mobile_backend.rs). So
// "keep the backend running while the app is in the background" means "keep this
// process alive", and on Android that is exactly what a foreground service is
// for. Without one the platform is free to kill the process minutes after the
// user switches away — Soulseek sharing and an in-flight import would stop.
//
// Why foregroundServiceType="specialUse": Android 14 requires every foreground
// service to declare a type from a fixed list, and a local media-organiser
// backend that serves P2P transfers and long imports fits none of the specific
// ones (`dataSync` is for uploads/downloads with a user-visible progress budget
// and Android 15 caps its runtime; `mediaPlayback` would be a lie unless audio
// is actually playing). `specialUse` is the type the platform provides for
// exactly this case, and the manifest declares the substring in
// PROPERTY_SPECIAL_USE_FGS_SUBTYPE for review.
//
// This file is NOT part of the repository's build: `gen/android` is generated in
// CI (`tauri android init`), so tools/mobile/android/inject.py copies this file,
// the manifest entries and the Gradle switch into the generated project — the
// same pattern the workflow already uses for the cleartext-traffic edit.
//
// The user's choice lives in a marker file the shell writes
// (`<app data dir>/keepalive`, i.e. `<context.dataDir>/keepalive`); Rust owns
// reading and writing it, this side only acts on it. A 5-second poll is how a
// toggle inside a running app takes effect without a JNI bridge between the two
// halves — it costs one stat per tick while the app is in the foreground, and
// stops itself the moment the marker is gone.
package __MLO_PACKAGE__

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import java.io.File

class MloKeepAliveService : Service() {
    private val handler = Handler(Looper.getMainLooper())

    private val poll = object : Runnable {
        override fun run() {
            if (!marker(this@MloKeepAliveService).exists()) {
                stopSelf()
                return
            }
            handler.postDelayed(this, POLL_MS)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIFICATION_ID, notification(),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, notification())
        }
        handler.removeCallbacks(poll)
        handler.postDelayed(poll, POLL_MS)
        // START_STICKY: if the platform kills the process anyway, it restarts the
        // service, which is the closest thing Android has to "keep hosting".
        return START_STICKY
    }

    override fun onDestroy() {
        handler.removeCallbacks(poll)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun notification(): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    "Background backend",
                    NotificationManager.IMPORTANCE_LOW,
                ).apply { description = "Keeps the local la musica backend running" }
            )
        }
        // Tapping the notification brings the app (and its UI) back to the front;
        // launchMode="singleTask" in the manifest is what keeps that from
        // creating a second activity.
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java).setFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setContentTitle("la musica is hosting your library")
            .setContentText("The local backend keeps running in the background")
            .setSmallIcon(android.R.drawable.stat_sys_upload_done)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    companion object {
        const val POLL_MS = 5_000L
        private const val CHANNEL_ID = "mlo-keepalive"
        private const val NOTIFICATION_ID = 4711

        /** The shell's setting: present means "keep hosting in the background". */
        fun marker(context: Context): File = File(context.dataDir, "keepalive")

        /** Start or stop the service so it matches the marker file. */
        fun sync(context: Context) {
            if (marker(context).exists()) start(context) else stop(context)
        }

        fun start(context: Context) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                context.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
                PackageManager.PERMISSION_GRANTED &&
                context is Activity
            ) {
                // A foreground service without a visible notification is not a
                // thing on Android 13+; asking here keeps the toggle honest.
                context.requestPermissions(
                    arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 4711
                )
            }
            context.startForegroundService(Intent(context, MloKeepAliveService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, MloKeepAliveService::class.java))
        }
    }
}
