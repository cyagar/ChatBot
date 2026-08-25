package com.hmwagner.techmanual.ui.history

import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.ApiService
import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
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

    private fun awaitState(timeoutMs: Long = 2000, predicate: (HistoryUiState) -> Boolean) {
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
}
