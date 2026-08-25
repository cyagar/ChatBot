package com.hmwagner.techmanual.network

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl

/**
 * Persistent cookie jar for the backend's httpOnly session cookie
 * (tma_session -- see backend/app/auth/deps.py). The cookie is encrypted at
 * rest with an AES-256-GCM key held in the Android Keystore (never leaves
 * the keystore, hardware-backed where the device supports it) before being
 * written to SharedPreferences -- previously it sat there in plain text,
 * readable via a rooted device or `adb backup`.
 *
 * This is NOT the plan's OIDC/PKCE token vault (plan section 8): there's
 * still no refresh token, no rotation, and no server-side per-device
 * revocation -- this only closes the "readable at rest" gap for the single
 * session cookie we already store. The fuller design is still blocked on an
 * identity-provider decision (plan section 18).
 */
class PersistentCookieJar(context: Context) : CookieJar {

    private val prefs = context.applicationContext.getSharedPreferences("cookie_jar", Context.MODE_PRIVATE)
    private val cache = mutableMapOf<String, MutableList<Cookie>>()

    init {
        // Pre-encryption installs stored a plaintext cookie under this same
        // key name. Discard rather than migrate it (android/README.md):
        // this is a demo with a known password, a one-time forced re-login
        // costs nothing, and plaintext-migration code would be dead weight
        // the moment every real install has upgraded past it.
        prefs.edit().remove(LEGACY_PLAINTEXT_KEY).apply()

        loadStoredCookie()?.let { cookie ->
            cache.getOrPut(cookie.domain) { mutableListOf() }.add(cookie)
        }
    }

    override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
        for (cookie in cookies) {
            if (cookie.name != SESSION_COOKIE_NAME) continue
            cache[cookie.domain] = mutableListOf(cookie)
            storeCookie(cookie)
        }
    }

    override fun loadForRequest(url: HttpUrl): List<Cookie> {
        val stored = cache[url.host] ?: return emptyList()
        return stored.filter { it.expiresAt > System.currentTimeMillis() }
    }

    fun clear() {
        cache.clear()
        prefs.edit().remove(KEY_IV).remove(KEY_CIPHERTEXT).apply()
    }

    fun hasSession(): Boolean = cache.values.any { list -> list.any { it.name == SESSION_COOKIE_NAME } }

    private fun storeCookie(cookie: Cookie) {
        val plaintext = CookieSerialization.serialize(cookie).toByteArray(Charsets.UTF_8)
        try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())
            val ciphertext = cipher.doFinal(plaintext)
            prefs.edit()
                .putString(KEY_IV, Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
                .putString(KEY_CIPHERTEXT, Base64.encodeToString(ciphertext, Base64.NO_WRAP))
                .apply()
        } catch (_: Exception) {
            // Encryption failing must not crash the login/send path -- the
            // in-memory cache still has the cookie for this process's
            // lifetime, it just won't survive a restart. Better than a
            // crash loop on a device with a broken Keystore.
        }
    }

    private fun loadStoredCookie(): Cookie? {
        val ivB64 = prefs.getString(KEY_IV, null) ?: return null
        val ciphertextB64 = prefs.getString(KEY_CIPHERTEXT, null) ?: return null
        return try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            val iv = Base64.decode(ivB64, Base64.NO_WRAP)
            cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(GCM_TAG_BITS, iv))
            val plaintext = cipher.doFinal(Base64.decode(ciphertextB64, Base64.NO_WRAP))
            CookieSerialization.parse(String(plaintext, Charsets.UTF_8))
        } catch (_: Exception) {
            // Corrupt ciphertext, a Keystore key that no longer works (lock
            // screen cleared, device-specific Keystore quirk), or any other
            // decrypt failure -- treat exactly like "no session" rather than
            // crashing on every request. Clear the unreadable entry so this
            // doesn't retry forever; the user just logs in again.
            prefs.edit().remove(KEY_IV).remove(KEY_CIPHERTEXT).apply()
            null
        }
    }

    private fun getOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }

        val keyGenerator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        keyGenerator.init(
            KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return keyGenerator.generateKey()
    }

    companion object {
        private const val SESSION_COOKIE_NAME = "tma_session"
        private const val LEGACY_PLAINTEXT_KEY = "session_cookie"
        private const val KEY_IV = "session_cookie_iv"
        private const val KEY_CIPHERTEXT = "session_cookie_ciphertext"
        private const val KEY_ALIAS = "techmanual_cookie_key"
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val GCM_TAG_BITS = 128
    }
}
