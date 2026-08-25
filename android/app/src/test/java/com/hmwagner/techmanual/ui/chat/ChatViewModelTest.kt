package com.hmwagner.techmanual.ui.chat

import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.ApiService
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
        server = MockWebServer()
        server.start()

        val retrofit = Retrofit.Builder()
            .baseUrl(server.url("/"))
            .client(OkHttpClient.Builder().retryOnConnectionFailure(false).build())
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
        ApiClient.overrideServiceForTest(retrofit.create(ApiService::class.java))

        // Consumed by ChatViewModel's init { refresh() }.
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
                request.path?.endsWith("/messages") == true -> {
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
    fun `successful feedback and save calls are reflected in state`() {
        server.enqueue(jsonResponse(
            """{"id": 5, "role": "assistant", "content": "Check the fuse.", "created_at": "2026-08-24T00:00:00Z"}"""
        ))
        vm.onComposerChange("Why won't it start?")
        vm.send()
        awaitState { !it.sending }

        server.enqueue(MockResponse().setResponseCode(204))
        vm.submitFeedback(5, "helpful")
        awaitState { it.feedbackGiven.containsKey(5) }
        assertEquals("helpful", vm.state.value.feedbackGiven[5])

        server.enqueue(MockResponse().setResponseCode(204))
        vm.saveAnswer(5)
        awaitState { it.savedMessageIds.contains(5) }
        assertTrue(vm.state.value.savedMessageIds.contains(5))
    }

    @Test
    fun `a failed feedback call is not recorded as given`() {
        server.enqueue(jsonResponse(
            """{"id": 6, "role": "assistant", "content": "Check the fuse.", "created_at": "2026-08-24T00:00:00Z"}"""
        ))
        vm.onComposerChange("Why won't it start?")
        vm.send()
        awaitState { !it.sending }

        server.enqueue(MockResponse().setResponseCode(500))
        vm.submitFeedback(6, "helpful")

        // No success signal to poll for on a failure -- give the (fast,
        // localhost) round trip a moment, then assert nothing was recorded.
        Thread.sleep(200)
        assertTrue(vm.state.value.feedbackGiven.isEmpty())
    }

    @Test
    fun `a failed feedback call surfaces an error instead of failing silently`() {
        // Regression test (2026-08-25, found via live tablet testing): this
        // used to swallow the exception/non-2xx entirely -- a technician
        // would tap Helpful, nothing would happen, and there was no
        // indication anything went wrong.
        server.enqueue(jsonResponse(
            """{"id": 7, "role": "assistant", "content": "Check the fuse.", "created_at": "2026-08-24T00:00:00Z"}"""
        ))
        vm.onComposerChange("Why won't it start?")
        vm.send()
        awaitState { !it.sending }

        server.enqueue(MockResponse().setResponseCode(500))
        vm.submitFeedback(7, "helpful")
        awaitState { it.error != null }
        assertTrue(vm.state.value.error!!.contains("feedback", ignoreCase = true))
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

    @Test
    fun `a 409 for an already-processing key triggers a refresh that resolves the pending echo`() {
        var askCount = 0
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path?.endsWith("/messages") == true && request.method == "POST" -> {
                    askCount++
                    MockResponse().setResponseCode(409)
                }
                request.path?.endsWith("/messages") == true && request.method == "GET" -> jsonResponse(
                    """[{"id": 9, "role": "assistant", "content": "OK", "created_at": "2026-08-24T00:00:00Z"}]"""
                )
                else -> MockResponse().setResponseCode(404)
            }
        }

        vm.onComposerChange("Why won't it start?")
        vm.send()
        awaitState { it.messages.isNotEmpty() }

        assertEquals(1, askCount)
        assertEquals(1, vm.state.value.messages.size)
        assertEquals(9, vm.state.value.messages[0].id)
        assertNull("loadMessages() found the reply, so the pending echo should be resolved, not left dangling", vm.state.value.pendingEcho)
    }
}
