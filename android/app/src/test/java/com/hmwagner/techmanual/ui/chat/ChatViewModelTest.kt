package com.hmwagner.techmanual.ui.chat

import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.ApiService
import com.hmwagner.techmanual.network.CitationOut
import com.hmwagner.techmanual.util.InMemoryPendingSendStore
import com.hmwagner.techmanual.util.PendingSendStore
import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import retrofit2.Retrofit

/**
 * Drives ChatViewModel against a real MockWebServer (via ApiClient's test
 * seam) instead of mocking ApiClient.service directly, so these exercise the
 * actual Retrofit/OkHttp/kotlinx.serialization wiring, not just the
 * ViewModel's own logic in isolation.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class ChatViewModelTest {

    private lateinit var server: MockWebServer
    private lateinit var vm: ChatViewModel
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    @Before
    fun setUp() {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        PendingSendStore.current = InMemoryPendingSendStore()
        server = MockWebServer()
        server.start()

        val retrofit = Retrofit.Builder()
            .baseUrl(server.url("/"))
            .client(OkHttpClient.Builder().retryOnConnectionFailure(false).build())
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
        ApiClient.overrideServiceForTest(retrofit.create(ApiService::class.java))

        // Consumed by ChatViewModel's init { refresh() }.
        server.enqueue(conversationJsonResponse())
        server.enqueue(jsonResponse("[]"))
        vm = ChatViewModel(conversationId = 1)
        awaitState { !it.loadingHistory }
    }

    @After
    fun tearDown() {
        server.shutdown()
        Dispatchers.resetMain()
    }

    private fun jsonResponse(body: String) =
        MockResponse().setResponseCode(200).setBody(body).addHeader("Content-Type", "application/json")

    // loadMessages() issues a GET .../conversations/{id} (see ChatViewModel)
    // before its GET .../messages -- every plain server.enqueue()-based (not
    // Dispatcher-based, which routes by path and needs no change)
    // refresh()/init trigger needs one more enqueued response ahead of the
    // messages one.
    private fun conversationJsonResponse(machineLabel: String? = null) = jsonResponse(
        """{"id": 1, "machine_id": null, "machine_label": ${machineLabel?.let { "\"$it\"" } ?: "null"},
            "started_at": "2026-08-24T00:00:00Z", "updated_at": "2026-08-24T00:00:00Z"}"""
    )

    private fun awaitState(timeoutMs: Long = 2000, predicate: (ChatUiState) -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (predicate(vm.state.value)) return
            Thread.sleep(5)
        }
        throw AssertionError("Timed out waiting for state condition. Last state: ${vm.state.value}")
    }

    @Test
    fun `send folds the user's own turn in before the assistant reply, not after`() {
        // Regression test for the bug where the technician's own question
        // rendered BELOW the assistant's answer -- pendingEcho was drawn in a
        // list item appended after `messages`, and send() only appended the
        // reply to `messages` without ever folding the user's turn into it.
        server.enqueue(jsonResponse(
            """{"id": 42, "role": "assistant", "content": "Replace the belt.", "created_at": "2026-08-24T00:00:00Z"}"""
        ))

        vm.onComposerChange("How do I replace the belt?")
        vm.send()
        awaitState { !it.sending }

        val state = vm.state.value
        assertNull(state.pendingEcho)
        assertEquals(2, state.messages.size)
        assertEquals("user", state.messages[0].role)
        assertEquals("How do I replace the belt?", state.messages[0].content)
        assertEquals("assistant", state.messages[1].role)
        assertEquals(42, state.messages[1].id)
    }

    @Test
    fun `losing connection mid-send marks the pending question uncertain instead of silently dropping it`() {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))

        vm.onComposerChange("Is this safe to run?")
        vm.send()
        awaitState { !it.sending }

        val state = vm.state.value
        assertFalse(state.sending)
        assertTrue("pendingEcho should survive so the question isn't silently lost", state.pendingEcho != null)
        assertEquals("Is this safe to run?", state.pendingEcho?.content)
        assertTrue(
            "pendingEchoUncertain should be set so the UI doesn't show a false 'still sending' spinner",
            state.pendingEchoUncertain,
        )
    }

    @Test
    fun `confirming a clarifying machine keeps the composer locked until the reload actually finishes`() {
        // Regression test for a race: selectClarifyingMachine() used to call
        // refresh(), which launches a DETACHED child coroutine and returns
        // immediately -- so its own `finally` cleared `sending` before that
        // child coroutine had actually reloaded anything, briefly re-enabling
        // the composer while the conversation was still mid-reload.
        val reachedReload = CountDownLatch(1)
        val releaseReload = CountDownLatch(1)

        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path?.endsWith("/machine") == true -> jsonResponse(
                    """{"id": 1, "machine_id": 7, "machine_label": "Acme 3000", "started_at": "2026-08-24T00:00:00Z", "updated_at": "2026-08-24T00:00:00Z"}"""
                )
                request.path?.contains("/messages") == true -> {
                    reachedReload.countDown()
                    releaseReload.await(2, TimeUnit.SECONDS)
                    jsonResponse("""[{"id": 99, "role": "assistant", "content": "Which model?", "created_at": "2026-08-24T00:00:00Z"}]""")
                }
                else -> MockResponse().setResponseCode(404)
            }
        }

        vm.selectClarifyingMachine(machineId = 7)

        assertTrue("reload request for the confirmed machine never arrived", reachedReload.await(2, TimeUnit.SECONDS))
        // The reload is now deliberately blocked mid-flight -- this is
        // exactly the window the old code got wrong.
        assertTrue("composer re-enabled while the post-confirmation reload was still in flight", vm.state.value.sending)
        assertTrue(vm.state.value.loadingHistory)

        releaseReload.countDown()
        awaitState { !it.sending }

        assertEquals(1, vm.state.value.messages.size)
        assertEquals(99, vm.state.value.messages[0].id)
    }

    @Test
    fun `a refresh landing while send is in flight cannot cause a duplicate message once send completes`() {
        // loadMessages() must not overwrite `messages` from a concurrent GET
        // while sendInFlight is true. If that GET already shows the server's
        // persisted answer to the question the client's own POST is still
        // awaiting a response for, performSend's success handler then
        // unconditionally appended its own synthetic user turn + the SAME
        // answer object on top -- the server-assigned answer id ended up in
        // `messages` twice, which LazyColumn(items, key={it.id}) treats as a
        // duplicate-key error. The POST is blocked behind a latch here so a
        // refresh() can land first with the answer already "persisted".
        val releaseSend = CountDownLatch(1)
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path?.endsWith("/messages") == true && request.method == "POST" -> {
                    releaseSend.await(2, TimeUnit.SECONDS)
                    jsonResponse("""{"id": 21, "role": "assistant", "content": "Check the fuse.", "created_at": "2026-08-24T00:00:00Z"}""")
                }
                request.path?.contains("/messages") == true && request.method == "GET" -> jsonResponse(
                    """[{"id": 20, "role": "user", "content": "Why won't it start?", "created_at": "2026-08-24T00:00:00Z"},
                        {"id": 21, "role": "assistant", "content": "Check the fuse.", "created_at": "2026-08-24T00:00:00Z"}]"""
                )
                else -> MockResponse().setResponseCode(404)
            }
        }

        vm.onComposerChange("Why won't it start?")
        vm.send()
        assertTrue("send() must still be in flight for this race to apply", vm.state.value.sending)

        vm.refresh()
        awaitState { !it.loadingHistory }

        releaseSend.countDown()
        awaitState { !it.sending }

        val ids = vm.state.value.messages.map { it.id }
        assertEquals("answer id 21 must appear exactly once, not duplicated", listOf(21), ids.filter { it == 21 })
        assertEquals("no duplicate ids anywhere in the list", ids.size, ids.toSet().size)
    }

    @Test
    fun `a failed send does not overwrite a newer draft typed while it was still in flight`() {
        // The failure handler must not restore the failed question's text
        // into the composer unconditionally. composerText is cleared to ""
        // only at the START of send() -- if the technician started typing
        // their NEXT question while this one was still failing server-side,
        // that newer draft is what's sitting in composerText when the
        // failure arrives, and overwriting it with the old failed text
        // would silently throw the newer draft away.
        val releaseSend = CountDownLatch(1)
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                releaseSend.await(2, TimeUnit.SECONDS)
                return MockResponse().setResponseCode(422)
            }
        }

        vm.onComposerChange("Why won't it start?")
        vm.send()
        assertTrue(vm.state.value.sending)

        // A newer draft, typed while the first question is still failing.
        vm.onComposerChange("Actually, a different question")

        releaseSend.countDown()
        awaitState { !it.sending }

        assertEquals(
            "the newer draft must survive the older request's failure callback",
            "Actually, a different question",
            vm.state.value.composerText,
        )
    }

    @Test
    fun `a successful save call is reflected in state`() {
        server.enqueue(jsonResponse(
            """{"id": 5, "role": "assistant", "content": "Check the fuse.", "created_at": "2026-08-24T00:00:00Z"}"""
        ))
        vm.onComposerChange("Why won't it start?")
        vm.send()
        awaitState { !it.sending }

        server.enqueue(MockResponse().setResponseCode(204))
        vm.saveAnswer(5)
        awaitState { it.savedMessageIds.contains(5) }
        assertTrue(vm.state.value.savedMessageIds.contains(5))
    }

    @Test
    fun `a network failure on save surfaces an error instead of failing silently`() {
        // "Connection: close" on the send response forces the save request
        // onto a genuinely fresh connection -- otherwise OkHttp transparently
        // retries a disconnect on a REUSED pooled connection (a routine
        // keep-alive race, unrelated to retryOnConnectionFailure), which
        // would silently mask exactly the failure this test means to trigger.
        server.enqueue(jsonResponse(
            """{"id": 8, "role": "assistant", "content": "Check the fuse.", "created_at": "2026-08-24T00:00:00Z"}"""
        ).addHeader("Connection", "close"))
        vm.onComposerChange("Why won't it start?")
        vm.send()
        awaitState { !it.sending }

        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))
        vm.saveAnswer(8)
        awaitState { it.error != null }
        assertTrue(vm.state.value.error!!.contains("save", ignoreCase = true))
        assertFalse(vm.state.value.savedMessageIds.contains(8))
    }

    @Test
    fun `a successful save clears a stale error left over from a prior failure`() {
        server.enqueue(jsonResponse(
            """{"id": 9, "role": "assistant", "content": "Check the fuse.", "created_at": "2026-08-24T00:00:00Z"}"""
        ))
        vm.onComposerChange("Why won't it start?")
        vm.send()
        awaitState { !it.sending }

        server.enqueue(MockResponse().setResponseCode(500))
        vm.saveAnswer(9)
        awaitState { it.error != null }

        server.enqueue(MockResponse().setResponseCode(204))
        vm.saveAnswer(9)
        awaitState { it.savedMessageIds.contains(9) }
        assertNull(vm.state.value.error)
    }

    @Test
    fun `a successful unsave call removes the message from saved state`() {
        server.enqueue(jsonResponse(
            """{"id": 10, "role": "assistant", "content": "Check the fuse.", "created_at": "2026-08-24T00:00:00Z"}"""
        ))
        vm.onComposerChange("Why won't it start?")
        vm.send()
        awaitState { !it.sending }

        server.enqueue(MockResponse().setResponseCode(200))
        vm.saveAnswer(10)
        awaitState { it.savedMessageIds.contains(10) }

        server.enqueue(MockResponse().setResponseCode(200))
        vm.unsaveAnswer(10)
        awaitState { !it.savedMessageIds.contains(10) }
        assertFalse(vm.state.value.savedMessageIds.contains(10))
    }

    @Test
    fun `a network failure on unsave surfaces an error and leaves saved state unchanged`() {
        server.enqueue(jsonResponse(
            """{"id": 11, "role": "assistant", "content": "Check the fuse.", "created_at": "2026-08-24T00:00:00Z"}"""
        ).addHeader("Connection", "close"))
        vm.onComposerChange("Why won't it start?")
        vm.send()
        awaitState { !it.sending }

        server.enqueue(MockResponse().setResponseCode(200).addHeader("Connection", "close"))
        vm.saveAnswer(11)
        awaitState { it.savedMessageIds.contains(11) }

        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))
        vm.unsaveAnswer(11)
        awaitState { it.error != null }
        assertTrue(vm.state.value.error!!.contains("saved answer", ignoreCase = true))
        assertTrue(vm.state.value.savedMessageIds.contains(11))
    }

    @Test
    fun `retryPendingSend reuses the original idempotency key instead of generating a new one`() {
        // Regression guard for the whole point of the key: a retry that used
        // a fresh UUID each time would be indistinguishable from the server
        // to a genuinely new question, defeating deduplication entirely.
        val seenKeys = mutableListOf<String?>()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.path?.endsWith("/messages") == true && request.method == "POST") {
                    seenKeys.add(request.getHeader("Idempotency-Key"))
                    return if (seenKeys.size == 1) {
                        MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START)
                    } else {
                        jsonResponse("""{"id": 9, "role": "assistant", "content": "OK", "created_at": "2026-08-24T00:00:00Z"}""")
                    }
                }
                return MockResponse().setResponseCode(404)
            }
        }

        vm.onComposerChange("Why won't it start?")
        vm.send()
        awaitState { !it.sending }
        assertTrue(vm.state.value.pendingEchoUncertain)

        vm.retryPendingSend()
        awaitState { !it.sending }

        assertEquals(2, seenKeys.size)
        assertEquals("first and retry Idempotency-Key must match", seenKeys[0], seenKeys[1])
        assertNull(vm.state.value.pendingEcho)
    }

    private fun conflict(code: String) = MockResponse().setResponseCode(409)
        .setBody("""{"detail": "conflict", "code": "$code", "correlation_id": "c1", "retryable": false}""")
        .addHeader("Content-Type", "application/json")

    @Test
    fun `an in-progress replay whose reload finds the reply resolves the pending question`() {
        var sentKey: String? = null
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path?.contains("/messages") == true && request.method == "POST" -> {
                    sentKey = request.getHeader("Idempotency-Key")
                    conflict("IDEMPOTENCY_IN_PROGRESS")
                }
                request.path?.contains("/messages") == true && request.method == "GET" -> jsonResponse(
                    """[{"id": 8, "role": "user", "content": "Why won't it start?", "idempotency_key": "$sentKey",
                         "created_at": "2026-08-24T00:00:00Z"},
                        {"id": 9, "role": "assistant", "content": "OK", "reply_to_message_id": 8,
                         "created_at": "2026-08-24T00:00:00Z"}]"""
                )
                else -> MockResponse().setResponseCode(404)
            }
        }

        vm.onComposerChange("Why won't it start?")
        vm.send()
        awaitState { it.messages.size == 2 }

        assertNull("the reply is correlated to this question, so the pending echo resolves", vm.state.value.pendingEcho)
        assertNull(PendingSendStore.current.load(1))
    }

    @Test
    fun `an in-progress replay whose reload finds only the stored question keeps it visibly pending`() {
        var sentKey: String? = null
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path?.contains("/messages") == true && request.method == "POST" -> {
                    sentKey = request.getHeader("Idempotency-Key")
                    conflict("IDEMPOTENCY_IN_PROGRESS")
                }
                request.path?.contains("/messages") == true && request.method == "GET" -> jsonResponse(
                    """[{"id": 10, "role": "user", "content": "Why won't it start?", "idempotency_key": "$sentKey",
                         "created_at": "2026-08-24T00:00:00Z"}]"""
                )
                else -> MockResponse().setResponseCode(404)
            }
        }

        vm.onComposerChange("Why won't it start?")
        vm.send()
        awaitState { !it.sending && !it.loadingHistory && it.pendingEchoStillProcessing }

        val state = vm.state.value
        assertEquals("Why won't it start?", state.pendingEcho?.content)
        assertFalse(state.pendingEchoUncertain)
        assertTrue("the stored copy must not also render as its own bubble", state.messages.none { it.role == "user" })
        assertTrue(state.error?.isNotBlank() == true)
        assertEquals("the pending question survives for a later retry", state.pendingEcho, PendingSendStore.current.load(1))
    }

    @Test
    fun `a busy-conversation 409 is a rejection, not proof the question was accepted`() {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path?.contains("/messages") == true && request.method == "POST" -> conflict("CONVERSATION_BUSY")
                else -> jsonResponse("[]")
            }
        }

        vm.onComposerChange("Second question")
        vm.send()
        awaitState { !it.sending }

        val state = vm.state.value
        assertNull(state.pendingEcho)
        assertEquals("the technician's text comes back to the composer", "Second question", state.composerText)
        assertNull(PendingSendStore.current.load(1))
    }

    @Test
    fun `a second question cannot be sent while the first has no answer yet`() {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))
        vm.onComposerChange("First question")
        vm.send()
        awaitState { !it.sending }
        assertTrue(vm.state.value.pendingEchoUncertain)

        vm.onComposerChange("Second question")
        vm.send()

        assertEquals("First question", vm.state.value.pendingEcho?.content)
        assertEquals("the second draft stays in the composer", "Second question", vm.state.value.composerText)
        assertEquals("no second request was made", 3, server.requestCount)
    }

    @Test
    fun `an unrelated older assistant reply never clears a pending question`() {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))
        vm.onComposerChange("Is this safe to run?")
        vm.send()
        awaitState { !it.sending }

        server.enqueue(conversationJsonResponse())
        server.enqueue(jsonResponse(
            """[{"id": 3, "role": "user", "content": "An older question", "idempotency_key": "old",
                 "created_at": "2026-08-24T00:00:00Z"},
                {"id": 4, "role": "assistant", "content": "An older answer", "reply_to_message_id": 3,
                 "created_at": "2026-08-24T00:00:00Z"}]"""
        ))
        vm.refresh()
        awaitState { !it.loadingHistory }

        assertEquals("Is this safe to run?", vm.state.value.pendingEcho?.content)
        assertTrue(vm.state.value.pendingEchoUncertain)
        assertEquals(2, vm.state.value.messages.size)
    }

    @Test
    fun `a pending question survives the ViewModel being recreated`() {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))
        vm.onComposerChange("Is this safe to run?")
        vm.send()
        awaitState { !it.sending }
        val original = vm.state.value.pendingEcho

        server.enqueue(conversationJsonResponse())
        server.enqueue(jsonResponse("[]"))
        vm = ChatViewModel(conversationId = 1)
        awaitState { !it.loadingHistory }

        assertEquals("same key and text, so a retry resumes the exact operation", original, vm.state.value.pendingEcho)
        assertTrue(vm.state.value.pendingEchoUncertain)
    }

    @Test
    fun `a reload response that arrives after a newer reload is discarded`() {
        val releaseFirst = CountDownLatch(1)
        val firstArrived = CountDownLatch(1)
        var getCount = 0
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.path?.contains("/messages") != true) return conversationJsonResponse()
                val n = synchronized(this) { ++getCount }
                if (n == 1) {
                    firstArrived.countDown()
                    releaseFirst.await(2, TimeUnit.SECONDS)
                    return jsonResponse(
                        """[{"id": 1, "role": "assistant", "content": "stale", "created_at": "2026-08-24T00:00:00Z"}]"""
                    )
                }
                return jsonResponse(
                    """[{"id": 2, "role": "assistant", "content": "fresh", "created_at": "2026-08-24T00:00:00Z"}]"""
                )
            }
        }

        vm.refresh()
        assertTrue(firstArrived.await(2, TimeUnit.SECONDS))
        vm.refresh()
        awaitState { it.messages.any { m -> m.content == "fresh" } }
        releaseFirst.countDown()
        Thread.sleep(200)

        assertEquals(listOf("fresh"), vm.state.value.messages.map { it.content })
    }

    @Test
    fun `earlier messages page in ahead of the newest page`() {
        val seenQueries = mutableListOf<String>()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.path?.contains("/messages") != true) return conversationJsonResponse()
                seenQueries.add(request.path.orEmpty())
                return if (request.path?.contains("before=CURSOR1") == true) {
                    jsonResponse(
                        """[{"id": 1, "role": "user", "content": "oldest", "created_at": "2026-08-24T00:00:00Z"}]"""
                    )
                } else {
                    jsonResponse(
                        """[{"id": 5, "role": "user", "content": "newest", "created_at": "2026-08-24T00:00:00Z"}]"""
                    ).addHeader("X-Next-Cursor", "CURSOR1").addHeader("X-Has-More", "true")
                }
            }
        }

        vm.refresh()
        awaitState { it.earlierCursor == "CURSOR1" }
        assertTrue(seenQueries.last().contains("latest=true"))

        vm.loadEarlier()
        awaitState { !it.loadingEarlier && it.messages.size == 2 }

        assertEquals(listOf("oldest", "newest"), vm.state.value.messages.map { it.content })
        assertNull("no older page remains", vm.state.value.earlierCursor)
    }

    @Test
    fun `refresh picks up a machine the server resolved for this conversation`() {
        // ChatScreen's toolbar must not read only the label passed through
        // navigation -- a snapshot from whenever the screen was opened,
        // never updated when the server resolves a machine for the
        // conversation through some other path (a mention in the
        // question). state.conversation is refreshed on every
        // loadMessages() call, not just after selectClarifyingMachine();
        // this pins that it actually reaches the ViewModel's state.
        assertNull("no machine resolved yet at setUp()", vm.state.value.conversation?.machine_label)

        server.enqueue(conversationJsonResponse(machineLabel = "Bunn-O-Matic Corporation Axiom"))
        server.enqueue(jsonResponse("[]"))
        vm.refresh()
        awaitState { !it.loadingHistory }

        assertEquals("Bunn-O-Matic Corporation Axiom", vm.state.value.conversation?.machine_label)
    }

    @Test
    fun `a failed conversation refresh does not block the messages themselves from loading`() {
        // Best-effort by design: the messages list is the primary content --
        // losing the machine-label refresh must never take that down with it,
        // and must not surface as a visible error either (setUp()'s own
        // initial fetch already left state.conversation non-null with a
        // null label -- this failure just means that goes unrefreshed).
        server.enqueue(MockResponse().setResponseCode(500))
        server.enqueue(jsonResponse(
            """[{"id": 30, "role": "assistant", "content": "Check the fuse.", "created_at": "2026-08-24T00:00:00Z"}]"""
        ))
        vm.refresh()
        awaitState { !it.loadingHistory }

        assertEquals(1, vm.state.value.messages.size)
        assertNull(vm.state.value.error)
    }

    @Test
    fun `refresh cannot clear an in-flight pending echo when nothing has been persisted yet`() {
        // A pull-to-refresh (ChatScreen's PullToRefreshBox calls the
        // same refresh() -> loadMessages() this test drives directly) must
        // never make an uncertain pending question look resolved just
        // because the reload happened to succeed -- an empty reload here
        // means the original POST may never have even reached the server.
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))
        vm.onComposerChange("Is this safe to run?")
        vm.send()
        awaitState { !it.sending }
        assertTrue(vm.state.value.pendingEchoUncertain)

        server.enqueue(conversationJsonResponse())
        server.enqueue(jsonResponse("[]"))
        vm.refresh()
        awaitState { !it.loadingHistory }

        assertTrue(
            "a bare refresh must not clear a still-uncertain pending echo",
            vm.state.value.pendingEcho != null,
        )
        assertTrue(vm.state.value.pendingEchoUncertain)
        assertFalse(
            "nothing was found persisted server-side -- this is the genuinely-uncertain case, not accepted-and-processing",
            vm.state.value.pendingEchoStillProcessing,
        )
    }

    @Test
    fun `a refresh landing while the original send is still in flight cannot misreport or clear its pendingEcho`() {
        // ChatScreen's PullToRefreshBox has no guard against pulling
        // to refresh while a send is genuinely still in flight (a real
        // answer takes 20-30s per ApiClient.kt's own comment, plenty of time
        // for an impatient pull). That reload's own GET can easily return
        // before the original POST does -- landing here must not touch
        // pendingEcho/its status at all; only the in-flight performSend()
        // coroutine owns this question's outcome.
        val reachedSend = CountDownLatch(1)
        val releaseSend = CountDownLatch(1)
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path?.endsWith("/messages") == true && request.method == "POST" -> {
                    reachedSend.countDown()
                    releaseSend.await(2, TimeUnit.SECONDS)
                    jsonResponse("""{"id": 11, "role": "assistant", "content": "OK", "created_at": "2026-08-24T00:00:00Z"}""")
                }
                request.path?.contains("/messages") == true && request.method == "GET" -> jsonResponse("[]")
                else -> MockResponse().setResponseCode(404)
            }
        }

        vm.onComposerChange("Why won't it start?")
        vm.send()
        assertTrue("send never reached the server", reachedSend.await(2, TimeUnit.SECONDS))
        assertTrue(vm.state.value.sending)

        vm.refresh()
        awaitState { !it.loadingHistory }

        val midState = vm.state.value
        assertTrue("the concurrent send's pendingEcho must survive an unrelated reload landing mid-flight", midState.pendingEcho != null)
        assertFalse(
            "must not show a false 'unknown if received' status over a send that's still genuinely in flight",
            midState.pendingEchoUncertain,
        )
        assertFalse(
            "must not show a false 'still processing' status over a send that's still genuinely in flight",
            midState.pendingEchoStillProcessing,
        )
        assertNull("must not show a false alarm banner over a send that's still genuinely in flight", midState.error)

        releaseSend.countDown()
        awaitState { !it.sending }
        assertNull("the original send must still resolve normally once it actually completes", vm.state.value.pendingEcho)
        assertEquals(2, vm.state.value.messages.size)
    }

    private val testCitation = CitationOut(chunk_id = 9, document_id = 3, filename = "manual.pdf", excerpt = "...")

    @Test
    fun `a non-2xx evidence response surfaces a visible, retryable error instead of silently closing the sheet`() {
        // openCitation must check isSuccessful, not just resp.body() -- the
        // body is simply null for a non-2xx response, so a bare null check
        // would leave evidence null and evidenceLoading false. ChatScreen
        // only shows the sheet for (evidenceLoading || evidence != null),
        // so a failed request would close it completely silently, with
        // nothing to retry. Covers 401, 403, 404, 500 -- all take the same
        // isSuccessful-false branch, so one loop is real coverage, not
        // four copies of the same assertion.
        for (code in listOf(401, 403, 404, 500)) {
            server.enqueue(MockResponse().setResponseCode(code))
            vm.openCitation(testCitation)
            awaitState { !it.evidenceLoading }

            val state = vm.state.value
            assertTrue("code $code should surface a visible error", state.evidenceError?.contains(code.toString()) == true)
            assertNull("a failed request must not report stale/empty evidence as real", state.evidence)
        }
    }

    @Test
    fun `a network failure loading evidence surfaces an error and does not silently close the sheet`() {
        // Same silent-failure bug as above, via the exception branch instead
        // of a non-2xx response -- covers what the plan calls "timeout": a
        // real SocketTimeoutException is caught by the same generic
        // `catch (Exception)` a connection-level disconnect is, the same
        // equivalence ChatViewModelTest's other "network failure" tests
        // already rely on.
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))

        vm.openCitation(testCitation)
        awaitState { !it.evidenceLoading }

        val state = vm.state.value
        assertEquals("Can't reach the server. Check your connection.", state.evidenceError)
        assertNull(state.evidence)
    }

    @Test
    fun `retryEvidence redrives the same citation and can recover after a prior failure`() {
        server.enqueue(MockResponse().setResponseCode(500))
        vm.openCitation(testCitation)
        awaitState { !it.evidenceLoading }
        assertTrue(vm.state.value.evidenceError != null)

        server.enqueue(jsonResponse(
            """{"chunk_id": 9, "content": "Turn off power before servicing.", "filename": "manual.pdf"}"""
        ))
        vm.retryEvidence()
        awaitState { !it.evidenceLoading }

        val state = vm.state.value
        assertNull("a successful retry must clear the earlier error", state.evidenceError)
        assertEquals("Turn off power before servicing.", state.evidence?.content)
    }

    @Test
    fun `dismissing an evidence error actually closes the sheet`() {
        // dismissEvidence used to only clear evidence/evidenceDocumentId --
        // if it left evidenceError set, the sheet's visibility condition
        // (evidenceLoading || evidence != null || evidenceError != null)
        // would keep it open, or a stale error would flash on the next,
        // unrelated citation tap.
        server.enqueue(MockResponse().setResponseCode(500))
        vm.openCitation(testCitation)
        awaitState { !it.evidenceLoading }
        assertTrue(vm.state.value.evidenceError != null)

        vm.dismissEvidence()

        val state = vm.state.value
        assertNull(state.evidenceError)
        assertNull(state.evidence)
        assertNull(state.evidenceDocumentId)
    }

    @Test
    fun `tapping citation B while A is still loading shows B's evidence, never A's stale response`() {
        // Each citation tap must cancel any previous load and check that a
        // response still belongs to the currently-open citation. A (chunk 1)
        // is delayed behind a latch; B (chunk 2) is tapped and resolves
        // first; A is then released and must NOT be allowed to overwrite
        // B's already-displayed evidence -- a direct safety risk, since
        // technicians use citations to verify manual instructions.
        val citationA = testCitation.copy(chunk_id = 1, document_id = 1)
        val citationB = testCitation.copy(chunk_id = 2, document_id = 2)
        val releaseA = CountDownLatch(1)

        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path?.contains("/1/chunks/1/") == true -> {
                    releaseA.await(2, TimeUnit.SECONDS)
                    jsonResponse("""{"chunk_id": 1, "content": "A's content", "filename": "a.pdf"}""")
                }
                request.path?.contains("/2/chunks/2/") == true ->
                    jsonResponse("""{"chunk_id": 2, "content": "B's content", "filename": "b.pdf"}""")
                else -> MockResponse().setResponseCode(404)
            }
        }

        vm.openCitation(citationA)
        vm.openCitation(citationB)
        awaitState { !it.evidenceLoading }

        assertEquals("B's content", vm.state.value.evidence?.content)
        assertEquals(2, vm.state.value.evidenceDocumentId)

        // A's delayed response now lands -- it must be ignored, not clobber B.
        releaseA.countDown()
        Thread.sleep(100)

        assertEquals("A's stale response overwrote B's evidence", "B's content", vm.state.value.evidence?.content)
        assertEquals(2, vm.state.value.evidenceDocumentId)
    }

    @Test
    fun `dismissing the sheet while a load is still in flight prevents it from reopening`() {
        // dismissEvidence must do more than clear state -- a response that
        // arrives after dismissal must not run its success handler and set
        // evidence/evidenceLoading again, or the sheet's own visibility
        // condition (evidenceLoading || evidence != null) would read that
        // as "reopen".
        val release = CountDownLatch(1)
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                release.await(2, TimeUnit.SECONDS)
                return jsonResponse("""{"chunk_id": 9, "content": "late content", "filename": "manual.pdf"}""")
            }
        }

        vm.openCitation(testCitation)
        assertTrue(vm.state.value.evidenceLoading)

        vm.dismissEvidence()
        assertNull(vm.state.value.evidence)
        assertFalse(vm.state.value.evidenceLoading)

        release.countDown()
        Thread.sleep(100)

        assertNull("a stale response reopened the dismissed sheet", vm.state.value.evidence)
        assertFalse(vm.state.value.evidenceLoading)
    }
}
