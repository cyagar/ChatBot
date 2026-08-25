package com.hmwagner.techmanual.ui.login

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
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import retrofit2.Retrofit

@OptIn(ExperimentalCoroutinesApi::class)
class LoginViewModelTest {

    private lateinit var server: MockWebServer
    private lateinit var vm: LoginViewModel
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
        vm = LoginViewModel()
    }

    @After
    fun tearDown() {
        server.shutdown()
        Dispatchers.resetMain()
    }

    private fun awaitState(timeoutMs: Long = 2000, predicate: (LoginUiState) -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (predicate(vm.state.value)) return
            Thread.sleep(5)
        }
        throw AssertionError("Timed out waiting for state condition. Last state: ${vm.state.value}")
    }

    @Test
    fun `blank credentials are rejected without ever hitting the network`() {
        vm.onEmailChange("")
        vm.onPasswordChange("")
        vm.login()

        assertEquals("Enter your email and password.", vm.state.value.error)
        assertEquals(0, server.requestCount)
    }

    @Test
    fun `successful login marks the user logged in`() {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setBody("""{"id": 1, "email": "tech.demo@hmwagner.com", "role": "technician"}""")
                .addHeader("Content-Type", "application/json"),
        )

        vm.onEmailChange("tech.demo@hmwagner.com")
        vm.onPasswordChange("DemoPass123!")
        vm.login()
        awaitState { !it.loading }

        assertTrue(vm.state.value.loggedIn)
        assertNull(vm.state.value.error)
    }

    @Test
    fun `wrong password surfaces a specific message and does not log in`() {
        server.enqueue(MockResponse().setResponseCode(401))

        vm.onEmailChange("tech.demo@hmwagner.com")
        vm.onPasswordChange("wrong")
        vm.login()
        awaitState { !it.loading }

        assertFalse(vm.state.value.loggedIn)
        assertEquals("Incorrect email or password.", vm.state.value.error)
    }

    @Test
    fun `lost connection during login surfaces a connection error, not a raw exception`() {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))

        vm.onEmailChange("tech.demo@hmwagner.com")
        vm.onPasswordChange("DemoPass123!")
        vm.login()
        awaitState { !it.loading }

        assertFalse(vm.state.value.loggedIn)
        assertEquals("Can't reach the server. Check your connection.", vm.state.value.error)
    }
}
