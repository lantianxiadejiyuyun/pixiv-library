package app.qiye.qiye_library

import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import java.io.File
import java.security.MessageDigest

class MainActivity : FlutterActivity() {
    @Suppress("DEPRECATION")
    private fun code(info: PackageInfo): Long = if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong()

    @Suppress("DEPRECATION")
    private fun signatures(info: PackageInfo): Set<String> =
        (if (Build.VERSION.SDK_INT >= 28) info.signingInfo?.apkContentsSigners else info.signatures)
            ?.map { bytesHash(it.toByteArray()) }?.toSet() ?: emptySet()

    private fun bytesHash(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes).joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun apkFile(): File = File(cacheDir, "apk-updates/update.apk")

    @Suppress("DEPRECATION")
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "app.qiye/updater").setMethodCallHandler { call, result ->
            val flags = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
            when (call.method) {
                "info" -> {
                    val info = packageManager.getPackageInfo(packageName, flags)
                    result.success(mapOf("version_name" to info.versionName, "version_code" to code(info),
                        "package_name" to packageName, "sdk" to Build.VERSION.SDK_INT,
                        "certificates" to signatures(info).toList()))
                }
                "prepare" -> {
                    try {
                        val file = apkFile()
                        file.parentFile!!.mkdirs()
                        if (file.exists() && !file.delete()) throw IllegalStateException()
                        result.success(file.absolutePath)
                    } catch (_: Exception) { result.error("storage", "无法准备更新目录，请检查手机存储", null) }
                }
                "install" -> {
                    val expectedCode = call.argument<Number>("version_code")?.toLong()
                    val expectedHash = call.argument<String>("sha256")
                    // Hashing a universal APK must not block Flutter's UI thread.
                    Thread {
                        try {
                            val file = apkFile()
                            if (!file.isFile || file.length() > 256L * 1024 * 1024) throw IllegalArgumentException()
                            val digest = MessageDigest.getInstance("SHA-256")
                            file.inputStream().use { stream ->
                                val buffer = ByteArray(1024 * 1024)
                                while (true) { val count = stream.read(buffer); if (count < 0) break; digest.update(buffer, 0, count) }
                            }
                            val hash = digest.digest().joinToString("") { "%02x".format(it.toInt() and 0xff) }
                            if (hash != expectedHash) throw IllegalArgumentException()
                            val installed = packageManager.getPackageInfo(packageName, flags)
                            val archive = packageManager.getPackageArchiveInfo(file.absolutePath, flags) ?: throw IllegalArgumentException()
                            val trusted = signatures(installed)
                            if (archive.packageName != packageName || code(archive) != expectedCode || code(archive) <= code(installed)
                                || trusted.isEmpty() || signatures(archive) != trusted) throw IllegalArgumentException()
                            runOnUiThread {
                                try {
                                    if (Build.VERSION.SDK_INT >= 26 && !packageManager.canRequestPackageInstalls()) {
                                        startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
                                        result.success("permission")
                                    } else {
                                        val uri = FileProvider.getUriForFile(this, "$packageName.apk-updates", file)
                                        startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
                                            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
                                        result.success("installer")
                                    }
                                } catch (_: Exception) { result.error("installer", "无法打开系统安装界面，请在网页下载安装包", null) }
                            }
                        } catch (_: Exception) { runOnUiThread { result.error("verification", "安装包版本或签名校验失败，请重新下载", null) } }
                    }.start()
                }
                else -> result.notImplemented()
            }
        }
    }
}
