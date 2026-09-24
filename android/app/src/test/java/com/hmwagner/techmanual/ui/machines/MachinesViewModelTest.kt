package com.hmwagner.techmanual.ui.machines

import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.ApiService
import com.hmwagner.techmanual.network.MachineOut
import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.TestCoroutineScheduler
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

@OptIn(ExperimentalCoroutinesApi::class)
class MachinesViewModelTest {

    private lateinit var server: MockWebServer
    private lateinit var vm: MachinesViewModel
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    // Explicit scheduler (rather than the default one UnconfinedTestDispatcher()
    // would create internally) so awaitState/awaitRequestCount below can
    // fast-forward past MachinesViewModel's real SEARCH_DEBOUNCE_MS delay()
    // -- without this, that delay() would suspend forever: nothing else in
    // these plain-JUnit tests (no runTest {}) ever advances virtual time.
    private val testScheduler = TestCoroutineScheduler()

    @Before
    fun setUp() {
        Dispatchers.setMain(UnconfinedTestDispatcher(testScheduler))
        server = MockWebServer()
        server.start()

        val retrofit = Retrofit.Builder()
            .baseUrl(server.url("/"))
            .client(OkHttpClient.Builder().retryOnConnectionFailure(false).build())
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
        ApiClient.overrideServiceForTest(retrofit.create(ApiService::class.java))
    }

    @After
    fun tearDown() {
        server.shutdown()
        Dispatchers.resetMain()
    }

    private fun jsonResponse(body: String) =
        MockResponse().setResponseCode(200).setBody(body).addHeader("Content-Type", "application/json")

    private fun awaitState(timeoutMs: Long = 2000, predicate: (MachinesUiState) -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            testScheduler.advanceUntilIdle()
            if (predicate(vm.state.value)) return
            Thread.sleep(5)
        }
        throw AssertionError("Timed out waiting for state condition. Last state: ${vm.state.value}")
    }

    private fun awaitRequestCount(n: Int, timeoutMs: Long = 2000) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            testScheduler.advanceUntilIdle()
            if (server.requestCount >= n) return
            Thread.sleep(5)
        }
        throw AssertionError("Timed out waiting for $n request(s); saw ${server.requestCount}")
    }

    @Test
    fun `loads recent machines on init`() {
        server.enqueue(jsonResponse(
            """[{"id": 1, "manufacturer": "Bunn", "model_name": "Axiom", "document_count": 3}]"""
        ))

        vm = MachinesViewModel()
        awaitState { it.recent.size == 1 }

        assertEquals("Bunn", vm.state.value.recent[0].manufacturer)
    }

    @Test
    fun `typing a query populates results`() {
        server.enqueue(jsonResponse("[]"))
        vm = MachinesViewModel()
        awaitRequestCount(1)

        server.enqueue(jsonResponse(
            """[{"id": 2, "manufacturer": "Hobart", "model_name": "HL600", "document_count": 5}]"""
        ))
        vm.onQueryChange("Hobart")
        awaitState { !it.loading }

        assertEquals(1, vm.state.value.results.size)
        assertEquals("Hobart", vm.state.value.results[0].manufacturer)
    }

    @Test
    fun `selecting a machine creates a conversation, touches it, and reports the label`() {
        server.enqueue(jsonResponse("[]"))
        vm = MachinesViewModel()
        awaitRequestCount(1)

        server.enqueue(jsonResponse(
            """{"id": 55, "machine_id": 1, "machine_label": "Bunn Axiom", "started_at": "2026-08-24T00:00:00Z", "updated_at": "2026-08-24T00:00:00Z"}"""
        ))
        server.enqueue(MockResponse().setResponseCode(200))

        val machine = MachineOut(id = 1, manufacturer = "Bunn", model_name = "Axiom", document_count = 3)
        var reportedId: Int? = null
        var reportedLabel: String? = null
        vm.selectMachine(machine) { id, label -> reportedId = id; reportedLabel = label }

        awaitState { !it.creatingConversation }

        assertEquals(55, reportedId)
        assertEquals("Bunn Axiom", reportedLabel)
    }

    @Test
    fun `starting without a machine reports a null label`() {
        server.enqueue(jsonResponse("[]"))
        vm = MachinesViewModel()
        awaitRequestCount(1)

        server.enqueue(jsonResponse(
            """{"id": 77, "machine_id": null, "machine_label": null, "started_at": "2026-08-24T00:00:00Z", "updated_at": "2026-08-24T00:00:00Z"}"""
        ))

        var called = false
        var reportedLabel: String? = "unset"
        vm.startWithoutMachine { _, label -> called = true; reportedLabel = label }

        awaitState { !it.creatingConversation }

        assertTrue(called)
        assertNull(reportedLabel)
    }

    @Test
    fun `pull-to-refresh with a blank query reloads recents`() {
        server.enqueue(jsonResponse("[]"))
        vm = MachinesViewModel()
        awaitRequestCount(1)

        server.enqueue(jsonResponse(
            """[{"id": 3, "manufacturer": "Hoshizaki", "model_name": "KM-515", "document_count": 2}]"""
        ))
        vm.refresh()
        awaitState { !it.refreshing }

        assertEquals(1, vm.state.value.recent.size)
        assertEquals("Hoshizaki", vm.state.value.recent[0].manufacturer)
        // Refreshing recents must not touch results, which stay whatever
        // they were (empty here, since no search was ever run).
        assertTrue(vm.state.value.results.isEmpty())
    }

    @Test
    fun `pull-to-refresh with an active query re-runs the search, not recents`() {
        server.enqueue(jsonResponse("[]"))
        vm = MachinesViewModel()
        awaitRequestCount(1)

        server.enqueue(jsonResponse("[]"))
        vm.onQueryChange("Hobart")
        awaitState { !it.loading }

        server.enqueue(jsonResponse(
            """[{"id": 4, "manufacturer": "Hobart", "model_name": "HL600", "document_count": 5}]"""
        ))
        vm.refresh()
        awaitState { !it.refreshing }

        assertEquals(1, vm.state.value.results.size)
        assertEquals("Hobart", vm.state.value.results[0].manufacturer)
    }

    @Test
    fun `a failed pull-to-refresh surfaces an error`() {
        server.enqueue(jsonResponse("[]"))
        vm = MachinesViewModel()
        awaitRequestCount(1)

        server.enqueue(MockResponse().setResponseCode(500))
        vm.refresh()
        awaitState { !it.refreshing }

        assertTrue(vm.state.value.error?.contains("500") == true)
    }

    @Test
    fun `a failed conversation creation surfaces an error and does not invoke the callback`() {
        server.enqueue(jsonResponse("[]"))
        vm = MachinesViewModel()
        awaitRequestCount(1)

        server.enqueue(MockResponse().setResponseCode(500))

        val machine = MachineOut(id = 1, manufacturer = "Bunn", model_name = "Axiom", document_count = 3)
        var called = false
        vm.selectMachine(machine) { _, _ -> called = true }

        awaitState { !it.creatingConversation }

        assertFalse(called)
        assertTrue(vm.state.value.error?.contains("500") == true)
    }

    @Test
    fun `search waits out the debounce before contacting the server`() {
        // A real request per keystroke is exactly what this must NOT
        // do -- assert the debounce delay genuinely holds the request back,
        // not just that the final result is eventually correct. Uses
        // takeRequest's own real (short) timeout, not an immediate
        // requestCount read racing the same real network call it's trying
        // to rule out.
        server.enqueue(jsonResponse("[]"))
        vm = MachinesViewModel()
        awaitRequestCount(1)
        server.takeRequest(200, TimeUnit.MILLISECONDS) // drain the init request

        server.enqueue(jsonResponse("[]"))
        vm.onQueryChange("Hobart")

        assertNull(
            "the debounce delay must not have elapsed yet -- no search request should be out",
            server.takeRequest(50, TimeUnit.MILLISECONDS),
        )

        awaitState { !it.loading }
        assertEquals(2, server.requestCount)
    }

    @Test
    fun `rapid typing before the debounce elapses fires only one search, for the final query`() {
        server.enqueue(jsonResponse("[]"))
        vm = MachinesViewModel()
        awaitRequestCount(1)

        server.enqueue(jsonResponse(
            """[{"id": 5, "manufacturer": "Hobart", "model_name": "HL600", "document_count": 5}]"""
        ))

        // Each call cancels the previous one's still-debouncing job before it
        // ever reaches the network -- none of "H"/"Ho"/"Hob" should generate
        // a request at all, only "Hobart".
        vm.onQueryChange("H")
        vm.onQueryChange("Ho")
        vm.onQueryChange("Hob")
        vm.onQueryChange("Hobart")
        awaitState { !it.loading }

        assertEquals("only the final keystroke's search should ever reach the server", 2, server.requestCount)
        assertEquals(1, vm.state.value.results.size)
        assertEquals("Hobart", vm.state.value.results[0].manufacturer)
    }

    @Test
    fun `a slow response for an older query cannot overwrite a newer query's results`() {
        // search() must cancel the previous coroutine on every keystroke --
        // an older, slower response landing AFTER a newer one must not
        // silently replace its results with stale data. Deterministic via a
        // blocking dispatcher (same pattern as
        // ChatViewModelTest's clarifying-machine race test), not timing
        // luck: the "H" response is held back on the server side until
        // after "Ho"'s has already been applied, then finally released.
        server.enqueue(jsonResponse("[]"))
        vm = MachinesViewModel()
        awaitRequestCount(1)

        val reachedStaleSearch = CountDownLatch(1)
        val releaseStaleSearch = CountDownLatch(1)
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val query = request.requestUrl?.queryParameter("q")
                return when (query) {
                    "H" -> {
                        reachedStaleSearch.countDown()
                        releaseStaleSearch.await(2, TimeUnit.SECONDS)
                        jsonResponse("""[{"id": 1, "manufacturer": "H-Stale", "model_name": "X", "document_count": 1}]""")
                    }
                    "Ho" -> jsonResponse(
                        """[{"id": 2, "manufacturer": "Hobart", "model_name": "HL600", "document_count": 5}]"""
                    )
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }

        vm.onQueryChange("H")
        testScheduler.advanceUntilIdle() // past the debounce, so "H" actually goes out now
        assertTrue("the older query's search never reached the server", reachedStaleSearch.await(2, TimeUnit.SECONDS))

        // Supersedes "H" -- its still-debouncing/in-flight job is cancelled,
        // and even if the network call somehow wasn't interrupted in time,
        // the query-match guard inside search() is the real backstop.
        vm.onQueryChange("Ho")
        awaitState { it.results.isNotEmpty() }

        assertEquals(1, vm.state.value.results.size)
        assertEquals("the newer query's result must win", "Hobart", vm.state.value.results[0].manufacturer)

        // Finally let the stale "H" response land -- it must be ignored,
        // not silently overwrite the correct "Ho" result now showing.
        releaseStaleSearch.countDown()
        Thread.sleep(150)

        assertEquals(1, vm.state.value.results.size)
        assertEquals("a stale response landing late must not replace current results", "Hobart", vm.state.value.results[0].manufacturer)
    }

    @Test
    fun `a touchMachine failure does not block navigation or report the conversation as failed`() {
        // touchMachine must not be awaited INSIDE selectMachine's own try
        // block, before onCreated -- a network exception there would report
        // total failure even though the conversation was already committed
        // server-side, so it would never open, and a retry could create a
        // second, empty conversation for the same machine.
        server.enqueue(jsonResponse("[]"))
        vm = MachinesViewModel()
        awaitRequestCount(1)

        // "Connection: close" forces the touchMachine request onto a
        // genuinely fresh connection -- confirmed via a throwaway diagnostic
        // that Response<Unit>'s converter never actually parses the body at
        // all (Retrofit/kotlinx-serialization special-cases Unit), so a
        // malformed body can't be used to trigger a failure here; only a
        // real connection-level exception can. Without this header, OkHttp
        // silently retries a disconnect on a REUSED pooled connection (a
        // routine keep-alive race, unrelated to retryOnConnectionFailure --
        // same behavior ApiClient.kt's getRetryInterceptor comment and
        // ChatViewModelTest's "a network failure on save..." test document),
        // which would mask exactly the failure this test means to trigger.
        server.enqueue(jsonResponse(
            """{"id": 66, "machine_id": 1, "machine_label": "Bunn Axiom", "started_at": "2026-08-24T00:00:00Z", "updated_at": "2026-08-24T00:00:00Z"}"""
        ).addHeader("Connection", "close"))
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START)) // touchMachine fails

        val machine = MachineOut(id = 1, manufacturer = "Bunn", model_name = "Axiom", document_count = 3)
        var reportedId: Int? = null
        vm.selectMachine(machine) { id, _ -> reportedId = id }

        awaitState { !it.creatingConversation }

        assertEquals("the already-committed conversation must still open", 66, reportedId)
        assertNull(
            "a best-effort touchMachine failure must never surface as a chat-blocking error",
            vm.state.value.error,
        )
    }

    @Test
    fun `refresh cancelling a still-debouncing search does not leave loading stuck`() {
        // search()'s debounce job must not be the ONLY place that clears
        // `loading`. refresh()'s searchJob?.cancel() -- needed so a
        // pull-to-refresh doesn't race a stale search -- could cancel that
        // job before it ever reached search(), stranding `loading = true`
        // forever even though the refresh itself completed successfully.
        server.enqueue(jsonResponse("[]"))
        vm = MachinesViewModel()
        awaitRequestCount(1)

        vm.onQueryChange("Hobart") // starts debouncing: loading = true, no request yet
        assertTrue(vm.state.value.loading)

        server.enqueue(jsonResponse(
            """[{"id": 6, "manufacturer": "Hobart", "model_name": "HL600", "document_count": 5}]"""
        ))
        vm.refresh() // cancels the still-debouncing search job before it ever fires
        awaitState { !it.refreshing }

        assertFalse("a cancelled debounce must not leave the spinner stuck", vm.state.value.loading)
        assertEquals(1, vm.state.value.results.size)
    }
}
