package com.hmwagner.techmanual.ui.history

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
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import retrofit2.Retrofit

@OptIn(ExperimentalCoroutinesApi::class)
class HistoryViewModelTest {

    private lateinit var server: MockWebServer
    private lateinit var vm: HistoryViewModel
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
    }

    @After
    fun tearDown() {
        server.shutdown()
        Dispatchers.resetMain()
    }

    private fun jsonResponse(body: String) =
        MockResponse().setResponseCode(200).setBody(body).addHeader("Content-Type", "application/json")

    private fun awaitState(timeoutMs: Long = 10000, predicate: (HistoryUiState) -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (predicate(vm.state.value)) return
            Thread.sleep(5)
        }
        throw AssertionError("Timed out waiting for state condition. Last state: ${vm.state.value}")
    }

    @Test
    fun `loads past conversations on init`() {
        server.enqueue(jsonResponse(
            """[{"id": 92, "machine_id": 14, "machine_label": "ASQ Glasswasher", "title": "Where is the drain valve",
                "started_at": "2026-08-25 14:25:45", "updated_at": "2026-08-25 14:26:10"}]"""
        ))

        vm = HistoryViewModel()
        // HistoryViewModel no longer auto-loads from init{} -- HistoryScreen's
        // LaunchedEffect(Unit) drives the first load instead (it must fire on
        // every composition entry, not just construction, to cover a
        // retained instance -- see the comment on that LaunchedEffect). These
        // ViewModel-only tests simulate that by calling refresh() explicitly.
        vm.refresh()
        awaitState { !it.loading }

        assertEquals(1, vm.state.value.conversations.size)
        assertEquals(92, vm.state.value.conversations[0].id)
        assertEquals("ASQ Glasswasher", vm.state.value.conversations[0].machine_label)
    }

    @Test
    fun `an empty history is not treated as an error`() {
        server.enqueue(jsonResponse("[]"))

        vm = HistoryViewModel()
        vm.refresh()
        awaitState { !it.loading }

        assertTrue(vm.state.value.conversations.isEmpty())
        assertEquals(null, vm.state.value.error)
    }

    @Test
    fun `a failed load surfaces an error`() {
        server.enqueue(MockResponse().setResponseCode(500))

        vm = HistoryViewModel()
        vm.refresh()
        awaitState { it.error != null }

        assertTrue(vm.state.value.error!!.contains("500"))
    }

    @Test
    fun `loadMore appends a second page using the cursor from the first response`() {
        server.enqueue(
            jsonResponse(
                """[{"id": 1, "machine_id": null, "machine_label": null, "title": "First",
                    "started_at": "2026-08-25 10:00:00", "updated_at": "2026-08-25 10:00:00"}]"""
            ).addHeader("X-Next-Cursor", "page2cursor").addHeader("X-Has-More", "true")
        )
        vm = HistoryViewModel()
        vm.refresh()
        awaitState { !it.loading }
        assertEquals("page2cursor", vm.state.value.nextCursor)

        server.enqueue(
            jsonResponse(
                """[{"id": 2, "machine_id": null, "machine_label": null, "title": "Second",
                    "started_at": "2026-08-25 09:00:00", "updated_at": "2026-08-25 09:00:00"}]"""
            ).addHeader("X-Has-More", "false")
        )
        vm.loadMore()
        awaitState { it.conversations.size == 2 }

        assertEquals(listOf(1, 2), vm.state.value.conversations.map { it.id })
        assertEquals(null, vm.state.value.nextCursor)

        server.takeRequest() // the initial refresh() call
        val secondRequest = server.takeRequest()
        assertTrue(secondRequest.path!!.contains("cursor=page2cursor"))
    }

    @Test
    fun `refresh replaces the list rather than appending to it`() {
        server.enqueue(jsonResponse(
            """[{"id": 1, "machine_id": null, "machine_label": null, "title": "First",
                "started_at": "2026-08-25 10:00:00", "updated_at": "2026-08-25 10:00:00"}]"""
        ))
        vm = HistoryViewModel()
        vm.refresh()
        awaitState { !it.loading }

        server.enqueue(jsonResponse(
            """[{"id": 2, "machine_id": null, "machine_label": null, "title": "Second",
                "started_at": "2026-08-25 11:00:00", "updated_at": "2026-08-25 11:00:00"}]"""
        ))
        vm.refresh()
        awaitState { it.conversations.any { c -> c.id == 2 } }

        assertEquals(1, vm.state.value.conversations.size)
        assertEquals(2, vm.state.value.conversations[0].id)
    }

    @Test
    fun `a refresh that completes while loadMore is still in flight supersedes it and clears loadingMore`() {
        server.enqueue(
            jsonResponse(
                """[{"id": 1, "machine_id": null, "machine_label": null, "title": "First",
                    "started_at": "2026-08-25 10:00:00", "updated_at": "2026-08-25 10:00:00"}]"""
            ).addHeader("X-Next-Cursor", "page2cursor")
        )
        vm = HistoryViewModel()
        vm.refresh()
        awaitState { !it.loading }
        assertEquals("page2cursor", vm.state.value.nextCursor)

        val loadMoreReachedServer = CountDownLatch(1)
        val releaseLoadMore = CountDownLatch(1)
        server.dispatcher = object : Dispatcher() {
            var dispatchCount = 0
            override fun dispatch(request: RecordedRequest): MockResponse {
                dispatchCount++
                return when (dispatchCount) {
                    // loadMore()'s request -- held open until the refresh()
                    // below has fully completed, reproducing "a NEWER
                    // refresh starts and finishes while an older loadMore is
                    // still genuinely in flight," not just issued earlier.
                    1 -> {
                        loadMoreReachedServer.countDown()
                        releaseLoadMore.await(2, TimeUnit.SECONDS)
                        jsonResponse(
                            """[{"id": 2, "machine_id": null, "machine_label": null, "title": "Second (stale)",
                                "started_at": "2026-08-25 09:00:00", "updated_at": "2026-08-25 09:00:00"}]"""
                        )
                    }
                    2 -> jsonResponse(
                        """[{"id": 99, "machine_id": null, "machine_label": null, "title": "Fresh",
                            "started_at": "2026-08-25 11:00:00", "updated_at": "2026-08-25 11:00:00"}]"""
                    )
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }

        vm.loadMore()
        assertTrue(loadMoreReachedServer.await(2, TimeUnit.SECONDS))
        assertTrue(vm.state.value.loadingMore)

        vm.refresh()
        awaitState { it.conversations.any { c -> c.id == 99 } }

        // Now let loadMore's stale response land.
        releaseLoadMore.countDown()
        Thread.sleep(150)

        assertEquals(
            "the stale page must never be appended onto the list refresh() already replaced",
            listOf(99),
            vm.state.value.conversations.map { it.id },
        )
        assertFalse("a superseded loadMore must not leave its spinner stuck", vm.state.value.loadingMore)
    }
}
