package com.hmwagner.techmanual.util

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import com.hmwagner.techmanual.ui.chat.LocalEcho
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The question a technician has sent but not yet seen answered, keyed by
 * conversation. Persisted so that process death between send and reply does
 * not lose the Idempotency-Key needed to resume the same operation.
 */
interface PendingSendStore {
    fun load(conversationId: Int): LocalEcho?
    fun save(conversationId: Int, echo: LocalEcho)
    fun clear(conversationId: Int)
    fun clearAll()

    companion object {
        /** Replaced with the disk-backed store in TechManualApp; JVM tests use the in-memory default. */
        @Volatile
        var current: PendingSendStore = InMemoryPendingSendStore()
    }
}

class InMemoryPendingSendStore : PendingSendStore {
    private val entries = mutableMapOf<Int, LocalEcho>()

    @Synchronized override fun load(conversationId: Int) = entries[conversationId]
    @Synchronized override fun save(conversationId: Int, echo: LocalEcho) { entries[conversationId] = echo }
    @Synchronized override fun clear(conversationId: Int) { entries.remove(conversationId) }
    @Synchronized override fun clearAll() = entries.clear()
}

/**
 * Disk-backed [PendingSendStore]. The question text is a technician's own
 * words about the equipment in front of them, so it gets the same
 * AES-256-GCM-via-Keystore treatment as the session cookie
 * (see network/PersistentCookieJar.kt) instead of sitting in SharedPreferences
 * as plain text -- readable via a rooted device or `adb backup` otherwise.
 * App sandboxing and allowBackup=false already reduce that exposure, but this
 * closes the "readable at rest" gap the same way the cookie jar does.
 */
class SharedPrefsPendingSendStore(context: Context) : PendingSendStore {
    private val prefs = context.applicationContext.getSharedPreferences("pending_sends", Context.MODE_PRIVATE)

    @Synchronized
    override fun load(conversationId: Int): LocalEcho? {
        val ivB64 = prefs.getString("iv_$conversationId", null) ?: return null
        val ciphertextB64 = prefs.getString("data_$conversationId", null) ?: return null
        return try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            val iv = Base64.decode(ivB64, Base64.NO_WRAP)
            cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(GCM_TAG_BITS, iv))
            val plaintext = String(cipher.doFinal(Base64.decode(ciphertextB64, Base64.NO_WRAP)), Charsets.UTF_8)
            val (id, content) = plaintext.split(SEPARATOR, limit = 2).let { it[0] to it[1] }
            LocalEcho(id, content)
        } catch (_: Exception) {
            // Corrupt ciphertext or a Keystore key that no longer works --
            // treat like "nothing pending" rather than crashing on launch.
            // Clears the unreadable entry so this doesn't retry forever; the
            // technician just re-asks, same as any other lost-draft case.
            clear(conversationId)
            null
        }
    }

    @Synchronized
    override fun save(conversationId: Int, echo: LocalEcho) {
        val plaintext = (echo.id + SEPARATOR + echo.content).toByteArray(Charsets.UTF_8)
        try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())
            val ciphertext = cipher.doFinal(plaintext)
            prefs.edit()
                .putString("iv_$conversationId", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
                .putString("data_$conversationId", Base64.encodeToString(ciphertext, Base64.NO_WRAP))
                .apply()
        } catch (_: Exception) {
            // Encryption failing must not crash the send path -- the
            // in-memory ChatViewModel state still has the echo for this
            // process's lifetime, it just won't survive a restart. Clears
            // any stale entry so a broken Keystore doesn't resurrect an old
            // pending send instead.
            clear(conversationId)
        }
    }

    @Synchronized
    override fun clear(conversationId: Int) {
        prefs.edit().remove("iv_$conversationId").remove("data_$conversationId").apply()
    }

    @Synchronized
    override fun clearAll() {
        prefs.edit().clear().apply()
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

    private companion object {
        // The Idempotency-Key (a UUID) never contains this, so a plain split
        // is enough -- no need for a length-prefixed or escaped encoding.
        const val SEPARATOR = "\u0000"
        const val KEY_ALIAS = "techmanual_pending_send_key"
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val GCM_TAG_BITS = 128
    }
}
