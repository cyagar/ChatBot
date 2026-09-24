package com.hmwagner.techmanual.util

import android.content.Context
import com.hmwagner.techmanual.ui.chat.LocalEcho

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

class SharedPrefsPendingSendStore(context: Context) : PendingSendStore {
    private val prefs = context.applicationContext.getSharedPreferences("pending_sends", Context.MODE_PRIVATE)

    override fun load(conversationId: Int): LocalEcho? {
        val id = prefs.getString("id_$conversationId", null) ?: return null
        val content = prefs.getString("content_$conversationId", null) ?: return null
        return LocalEcho(id, content)
    }

    override fun save(conversationId: Int, echo: LocalEcho) {
        prefs.edit().putString("id_$conversationId", echo.id).putString("content_$conversationId", echo.content).apply()
    }

    override fun clear(conversationId: Int) {
        prefs.edit().remove("id_$conversationId").remove("content_$conversationId").apply()
    }

    override fun clearAll() {
        prefs.edit().clear().apply()
    }
}
