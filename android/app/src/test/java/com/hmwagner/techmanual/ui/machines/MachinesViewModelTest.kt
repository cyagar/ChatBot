package com.hmwagner.techmanual.ui.machines

import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.ApiService
import com.hmwagner.techmanual.network.MachineOut
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

    private fun awaitState(timeoutMs: Long = 2000, predicate: (MachinesUiState) -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (predicate(vm.state.value)) return
            Thread.sleep(5)
        }
        throw AssertionError("Timed out waiting for state condition. Last state: ${vm.state.value}")
    }

    private fun awaitRequestCount(n: Int, timeoutMs: Long = 2000) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
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
}
