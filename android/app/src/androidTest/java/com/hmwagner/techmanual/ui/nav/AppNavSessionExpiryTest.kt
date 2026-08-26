package com.hmwagner.techmanual.ui.nav

import androidx.compose.material3.windowsizeclass.ExperimentalMaterial3WindowSizeClassApi
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.LoginRequest
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Covers the one AppNav behavior that isn't a JVM unit test away: the
 * `sessionExpired` redirect in AppNav.kt (see the `LaunchedEffect(sessionExpired)`
 * block). That flag is flipped by ApiClient's `authExpiryInterceptor`, which
 * only runs when ApiClient.init/initForTest builds a real OkHttp pipeline --
 * the JVM ViewModel tests (e.g. LoginViewModelTest) build their own bare
 * Retrofit client against MockWebServer and never wire that interceptor up
 * at all, so this path has never actually been exercised by a test before.
 *
 * Runs on-device/emulator because PersistentCookieJar's encryption depends
 * on the real Android Keystore, which the JVM test tree doesn't have.
 */
@OptIn(ExperimentalMaterial3WindowSizeClassApi::class)
@RunWith(AndroidJUnit4::class)
class AppNavSessionExpiryTest {

    @get:Rule
    val composeTestRule = createComposeRule()

    private lateinit var server: MockWebServer

    // Compact width -> AppNav's HomeContent renders SinglePaneHome, whose
    // start destination (Machines) is what actually issues the request that
    // triggers the 401 below.
    private val compactWindowSizeClass = WindowSizeClass.calculateFromSize(DpSize(400.dp, 800.dp))

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()

        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        ApiClient.initForTest(context, server.url("/").toString())
        // A prior instrumented test run on this same device/emulator may have
        // left a real session cookie in PersistentCookieJar's encrypted
        // SharedPreferences -- clear it so hasSession() starts this test
        // false, matching a fresh install.
        ApiClient.clearSession()

        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Set-Cookie", "tma_session=faketoken; Path=/; HttpOnly")
                .setHeader("Content-Type", "application/json")
                .setBody("""{"id": 1, "email": "tech.demo@hmwagner.com", "role": "technician"}"""),
        )
        runBlocking {
            val response = ApiClient.service.login(LoginRequest("tech.demo@hmwagner.com", "DemoPass123!"))
            assertTrue("fake login must succeed for the test to start from a real session", response.isSuccessful)
        }
        assertTrue("PersistentCookieJar must have stored the session cookie from the fake login", ApiClient.hasSession())
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun a401OnTheFirstAuthenticatedRequestRedirectsToLogin() {
        // MachinesViewModel.init{} fires GET api/machines/recent the moment
        // MachinesScreen enters composition -- this is the only request
        // AppNav's Home destination issues on first render, so this one
        // enqueued 401 is what the authExpiryInterceptor sees.
        server.enqueue(MockResponse().setResponseCode(401))

        composeTestRule.setContent {
            AppNav(windowSizeClass = compactWindowSizeClass)
        }

        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText("Technician Manual Assistant").fetchSemanticsNodes().isNotEmpty()
        }
        composeTestRule.onNodeWithText("Technician Manual Assistant").assertExists()

        assertFalse("the cleared session must not still look logged in after the redirect", ApiClient.hasSession())
        assertFalse("AppNav must have consumed the flag via onSessionExpiredHandled()", ApiClient.sessionExpired.value)
    }

    @Test
    fun aSuccessfulAuthenticatedRequestDoesNotRedirectToLogin() {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("[]"),
        )

        composeTestRule.setContent {
            AppNav(windowSizeClass = compactWindowSizeClass)
        }

        // Positive control: assert Home (MachinesScreen's app bar title, the
        // one thing on screen regardless of whether the recent-machines list
        // came back empty) actually rendered, not just that Login didn't --
        // an assertion that only checks Login's absence would also pass on a
        // blank screen or a request that silently never fired.
        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText("Ask about a machine").fetchSemanticsNodes().isNotEmpty()
        }
        composeTestRule.onNodeWithText("Ask about a machine").assertExists()
        assertEquals(
            "expected exactly the fake login plus one recentMachines() call",
            2,
            server.requestCount,
        )

        val loginNodes = composeTestRule.onAllNodesWithText("Technician Manual Assistant").fetchSemanticsNodes()
        assertTrue("a successful request must not trigger the session-expiry redirect", loginNodes.isEmpty())
        assertTrue(ApiClient.hasSession())
    }
}
