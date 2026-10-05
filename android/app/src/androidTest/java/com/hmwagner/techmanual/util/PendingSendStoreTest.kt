package com.hmwagner.techmanual.util

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.hmwagner.techmanual.ui.chat.LocalEcho
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Runs on-device, the same reason PersistentCookieJar's tests do: the AES
 * -GCM encryption in SharedPrefsPendingSendStore needs the real Android
 * Keystore, which a plain JVM unit test cannot provide.
 */
@RunWith(AndroidJUnit4::class)
class PendingSendStoreTest {

    private lateinit var context: Context

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        // A clean slate for every test, including whatever a prior test
        // (or a prior run that crashed mid-test) left behind -- this file
        // is shared, real on-device storage, not reset between test runs
        // the way an in-memory fake would be.
        context.getSharedPreferences("pending_sends", Context.MODE_PRIVATE).edit().clear().commit()
    }

    @Test
    fun savedEntryRoundTripsThroughLoad() {
        val store = SharedPrefsPendingSendStore(context)
        val echo = LocalEcho("idem-key-1", "What is the part number?")
        store.save(42, echo)

        val loaded = store.load(42)
        assertEquals(echo.id, loaded?.id)
        assertEquals(echo.content, loaded?.content)
    }

    @Test
    fun clearRemovesTheSavedEntry() {
        val store = SharedPrefsPendingSendStore(context)
        store.save(42, LocalEcho("idem-key-1", "What is the part number?"))
        store.clear(42)
        assertNull(store.load(42))
    }

    @Test
    fun aLegacyPlaintextEntryFromBeforeEncryptionWasAddedIsDeletedOnConstruction() {
        // Reproduces the shape a prior, pre-encryption version of this store
        // wrote directly (see PendingSendStore.kt's own migration comment):
        // the question's id/content as plain strings, not the iv_/data_
        // ciphertext pair the current version reads and writes.
        context.getSharedPreferences("pending_sends", Context.MODE_PRIVATE).edit()
            .putString("id_7", "idem-key-legacy")
            .putString("content_7", "This was sitting in plain text on disk.")
            .commit()

        SharedPrefsPendingSendStore(context) // construction alone must run the migration

        val prefs = context.getSharedPreferences("pending_sends", Context.MODE_PRIVATE)
        assertNull("a legacy plaintext id_ key must not survive construction", prefs.getString("id_7", null))
        assertNull(
            "a legacy plaintext content_ key must not survive construction",
            prefs.getString("content_7", null),
        )
    }

    @Test
    fun aLegacyPlaintextEntryDoesNotInterfereWithAFreshEncryptedSaveUnderTheSameConversationId() {
        context.getSharedPreferences("pending_sends", Context.MODE_PRIVATE).edit()
            .putString("id_9", "idem-key-legacy")
            .putString("content_9", "stale plaintext draft")
            .commit()

        val store = SharedPrefsPendingSendStore(context)
        val echo = LocalEcho("idem-key-fresh", "A brand new question.")
        store.save(9, echo)

        val loaded = store.load(9)
        assertEquals(echo.id, loaded?.id)
        assertEquals(echo.content, loaded?.content)
    }
}
