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
        // A plaintext cookie stored under this key by an older install is
        // discarded, forcing one re-login, rather than migrated.
        prefs.edit().remove(LEGACY_PLAINTEXT_KEY).apply()

        loadStoredCookie()?.let { cookie ->
            cache.getOrPut(cookie.domain) { mutableListOf() }.add(cookie)
        }
    }

    // OkHttp calls a CookieJar from whatever thread is running that
    // request's dispatch -- concurrent requests (a background refresh
    // racing a user-initiated tap, for instance) could read/write `cache`
    // at the same time with no lock. @Synchronized on every method touching
    // it (on `this`, the single
    // instance OkHttpClient.Builder holds) is enough here: none of these
    // methods call each other, so there's no reentrancy/deadlock risk to
    // reason about.
    @Synchronized
    override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
        for (cookie in cookies) {
            if (cookie.name != SESSION_COOKIE_NAME) continue
            cache[cookie.domain] = mutableListOf(cookie)
            storeCookie(cookie)
        }
    }

    @Synchronized
    override fun loadForRequest(url: HttpUrl): List<Cookie> {
        // Checked against Cookie.matches()'s full path/secure/domain-subdomain
        // rules, not just host+expiry -- this app only ever has one cookie
        // for one host today, but a future host/path change (or a cookie
        // this jar doesn't fully control) could otherwise send a cookie
        // somewhere RFC 6265 says it shouldn't go.
        return cache.values.flatten().filter { it.expiresAt > System.currentTimeMillis() && it.matches(url) }
    }

    @Synchronized
    fun clear() {
        cache.clear()
        prefs.edit().remove(KEY_IV).remove(KEY_CIPHERTEXT).apply()
    }

    @Synchronized
    fun hasSession(): Boolean {
        // Checks expiresAt -- an expired cookie must not read as "has a
        // session", or AppNav's startup /me call (which exists specifically
        // to catch a session the server no longer honors) would run for a
        // cookie that was never going to be sent in the first place once
        // loadForRequest's own expiry filter (above) excludes it, an
        // avoidable network round trip on a launch that could go straight
        // to SignedOut.
        val now = System.currentTimeMillis()
        return cache.values.any { list -> list.any { it.name == SESSION_COOKIE_NAME && it.expiresAt > now } }
    }

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
            //
            // Also clears whatever ciphertext was already persisted (from
            // an earlier, successful encryption) -- otherwise a restart's
            // loadStoredCookie would decrypt and restore that OLD session,
            // one this in-memory cache had already moved past (e.g. the
            // user logged in again as someone else, or the session was
            // cleared, between that old encrypt and this failed one),
            // silently resurrecting a session this process no longer
            // believes is current. A broken Keystore loses persistence
            // entirely instead of persisting stale data.
            prefs.edit().remove(KEY_IV).remove(KEY_CIPHERTEXT).apply()
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
