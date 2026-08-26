package com.hmwagner.techmanual.ui.nav

import androidx.compose.material3.windowsizeclass.ExperimentalMaterial3WindowSizeClassApi
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
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
        // logoutClearsTheLocalSessionEvenWhenTheServerIsUnreachable() below
        // shuts the server down itself to simulate an unreachable server --
        // guard against a double-shutdown throwing here in that case.
        try {
            server.shutdown()
        } catch (_: Exception) {
        }
    }

    @Test
    fun a401OnTheFirstAuthenticatedRequestRedirectsToLogin() {
        // AppNav's own launch-time /me check (P0A-1) runs first and must
        // succeed here so Home actually mounts -- this test is about the
        // 401 that MachinesViewModel.init{}'s GET api/machines/recent gets
        // once MachinesScreen enters composition, not about /me itself.
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"id": 1, "email": "tech.demo@hmwagner.com", "role": "technician"}"""),
        )
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
                .setBody("""{"id": 1, "email": "tech.demo@hmwagner.com", "role": "technician"}"""),
        )
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
            "expected exactly the fake login, AppNav's launch-time /me check, plus one recentMachines() call",
            3,
            server.requestCount,
        )

        val loginNodes = composeTestRule.onAllNodesWithText("Technician Manual Assistant").fetchSemanticsNodes()
        assertTrue("a successful request must not trigger the session-expiry redirect", loginNodes.isEmpty())
        assertTrue(ApiClient.hasSession())
    }

    // P0A-1 exit gate: "No state, label, message, evidence, saved status, or
    // selection from one account is visible after another account signs in."
    // Drives a real conversation open via UI clicks (not just setting
    // HomeSelectionViewModel fields directly), then forces a session expiry
    // and asserts that logging back in -- even as the SAME account, which is
    // the harder case since a stale label collision wouldn't be visible as an
    // obviously wrong account -- lands back on the machine list, not straight
    // into the old conversation. Before this fix, AppNav's sessionExpired
    // handler left HomeSelectionViewModel untouched (it's Activity-scoped,
    // not tied to the HOME back-stack entry the way the NavHost-cleared
    // ViewModels are), so a fresh login could reopen the old conversation.
    @Test
    fun sessionExpiryClearsTheSelectedConversationSoALaterLoginStartsClean() {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"id": 1, "email": "tech.demo@hmwagner.com", "role": "technician"}"""),
        )
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("""[{"id": 7, "manufacturer": "Acme", "model_name": "X100", "family": null, "document_count": 1, "is_favorite": false}]"""),
        )
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"id": 42, "machine_id": 7, "machine_label": "Acme X100", "title": null, "started_at": "", "updated_at": ""}"""),
        )
        // No body, matching MachinesViewModelTest's proven-working
        // touchMachine mock -- Response<Unit>'s body converter behavior with
        // an explicit empty-object body was never verified here.
        server.enqueue(MockResponse().setResponseCode(200)) // touchMachine
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("[]"),
        ) // ChatViewModel's initial getMessages

        composeTestRule.setContent {
            AppNav(windowSizeClass = compactWindowSizeClass)
        }

        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText("Acme X100").fetchSemanticsNodes().isNotEmpty()
        }
        composeTestRule.onNodeWithText("Acme X100").performClick()

        // Chat's top bar renders the machine label next to the "Chat" title
        // (see ChatScreen's TopAppBar) once the conversation is actually open.
        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText("Chat").fetchSemanticsNodes().isNotEmpty()
        }

        // Exercises the exact same AppNav-level handling a real 401 would
        // (see ApiClient.sessionExpired's doc comment). logout() makes a
        // real POST /api/auth/logout call first -- MockWebServer's queue
        // blocks waiting for a response if none is enqueued, so this needs
        // its own response even though the test doesn't care what it is.
        server.enqueue(MockResponse().setResponseCode(200))
        runBlocking { ApiClient.logout() }

        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText("Technician Manual Assistant").fetchSemanticsNodes().isNotEmpty()
        }

        // Re-login through the real LoginScreen UI, not a direct service
        // call -- onLoggedIn -> navController.navigate(Routes.HOME) is what
        // actually exercises whether HomeContent starts clean.
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Set-Cookie", "tma_session=faketoken2; Path=/; HttpOnly")
                .setHeader("Content-Type", "application/json")
                .setBody("""{"id": 1, "email": "tech.demo@hmwagner.com", "role": "technician"}"""),
        )
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("[]"),
        ) // recentMachines() on the freshly re-entered Home

        composeTestRule.onNodeWithText("Email").performTextInput("tech.demo@hmwagner.com")
        composeTestRule.onNodeWithText("Password").performTextInput("DemoPass123!")
        composeTestRule.onNodeWithText("Sign in").performClick()

        // If HomeSelectionViewModel's selection had NOT been cleared,
        // SinglePaneHome would start straight on the Chat route again (see
        // its `startDestination`) and this title would never render.
        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText("Ask about a machine").fetchSemanticsNodes().isNotEmpty()
        }
        composeTestRule.onNodeWithText("Ask about a machine").assertExists()
    }

    // P0A-1 exit gate + test list item 10: a technician must always be able
    // to sign out of THIS device, even if the server can't be reached to
    // revoke the session server-side.
    @Test
    fun logoutClearsTheLocalSessionEvenWhenTheServerIsUnreachable() {
        server.shutdown()

        runBlocking { ApiClient.logout() }

        assertFalse("logout must clear the local session even when the server call fails", ApiClient.hasSession())
    }
}
