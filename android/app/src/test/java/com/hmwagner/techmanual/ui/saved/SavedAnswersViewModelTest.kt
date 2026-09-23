package com.hmwagner.techmanual.ui.saved

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
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import retrofit2.Retrofit

@OptIn(ExperimentalCoroutinesApi::class)
class SavedAnswersViewModelTest {

    private lateinit var server: MockWebServer
    private lateinit var vm: SavedAnswersViewModel
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
        vm = SavedAnswersViewModel()
    }

    @After
    fun tearDown() {
        server.shutdown()
        Dispatchers.resetMain()
    }

    private fun jsonResponse(body: String) =
        MockResponse().setResponseCode(200).setBody(body).addHeader("Content-Type", "application/json")

    private fun awaitState(timeoutMs: Long = 2000, predicate: (SavedAnswersUiState) -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (predicate(vm.state.value)) return
            Thread.sleep(5)
        }
        throw AssertionError("Timed out waiting for state condition. Last state: ${vm.state.value}")
    }

    private fun savedAnswerJson(messageId: Int) = """
        {"conversation_id": 1, "machine_label": "ASQ Glasswasher", "question": "Where is the drain valve",
         "answer": {"id": $messageId, "role": "assistant", "content": "Check the base.", "created_at": "2026-08-24T00:00:00Z"}}
    """.trimIndent()

    @Test
    fun `unsave optimistically removes the row before the server confirms it`() {
        server.enqueue(jsonResponse("[${savedAnswerJson(5)}]"))
        vm.refresh()
        awaitState { !it.loading }
        assertEquals(1, vm.state.value.answers.size)

        server.enqueue(MockResponse().setResponseCode(200))
        vm.unsave(5)
        awaitState { it.answers.isEmpty() }
        assertTrue(vm.state.value.answers.isEmpty())
    }

    @Test
    fun `a failed unsave restores the removed row and surfaces an error`() {
        server.enqueue(jsonResponse("[${savedAnswerJson(6)}]").addHeader("Connection", "close"))
        vm.refresh()
        awaitState { !it.loading }

        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))
        vm.unsave(6)
        awaitState { it.error != null }

        assertEquals(1, vm.state.value.answers.size)
        assertEquals(6, vm.state.value.answers[0].answer.id)
        assertNotNull(vm.state.value.error)
    }
}
