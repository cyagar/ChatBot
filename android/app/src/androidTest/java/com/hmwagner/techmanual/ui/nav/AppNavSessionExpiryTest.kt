package com.hmwagner.techmanual.ui.nav

import androidx.compose.material3.windowsizeclass.ExperimentalMaterial3WindowSizeClassApi
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.ui.test.assertCountEquals
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
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.DelicateCoroutinesApi
import kotlinx.coroutines.GlobalScope
import kotlinx.coroutines.launch
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

    // AppNav's own LaunchedEffect(Unit) fetches GET /api/config as its
    // first request on every composition -- every test below that calls
    // composeTestRule.setContent { AppNav(...) }
    // must enqueue this first, ahead of whatever that test's own scenario
    // needs, the same way setUp()'s fake login already comes first.
    private fun configOkResponse() = MockResponse().setResponseCode(200)
        .setHeader("Content-Type", "application/json")
        .setBody(
            """{"maintenance_mode": false, "maintenance_message": "", "minimum_supported_version": "0.0.0",
                "support_contact": "", "status": "ok", "status_message": ""}""",
        )

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
        // AppNav's own launch-time /me check runs first and must
        // succeed here so Home actually mounts -- this test is about the
        // 401 that MachinesViewModel.init{}'s GET api/machines/recent gets
        // once MachinesScreen enters composition, not about /me itself.
        server.enqueue(configOkResponse())
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

    // The launch-time /me check must not treat ANY non-2xx response
    // identically to a 401 -- a transient outage (500/503/429) during cold
    // launch must not sign a technician out of a perfectly valid session,
    // the same as a genuinely revoked one would be.
    @Test
    fun aTransientServerErrorOnTheStartupMeCallDoesNotSignOut() {
        server.enqueue(configOkResponse())
        server.enqueue(MockResponse().setResponseCode(503))
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("[]"),
        ) // MachinesViewModel's recentMachines(), once Home mounts as SignedIn

        composeTestRule.setContent {
            AppNav(windowSizeClass = compactWindowSizeClass)
        }

        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText("Ask about a machine").fetchSemanticsNodes().isNotEmpty()
        }
        composeTestRule.onNodeWithText("Ask about a machine").assertExists()
        assertTrue(
            "a transient 503 at launch must not sign the technician out of a valid session",
            ApiClient.hasSession(),
        )
    }

    // maintenance_mode from GET /api/config must block the app entirely
    // -- not just Home, since there
    // is nothing useful to do at Login either during an incident.
    @Test
    fun maintenanceModeBlocksTheAppBeforeHomeOrLoginRenders() {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"maintenance_mode": true, "maintenance_message": "Down for scheduled maintenance.",
                        "minimum_supported_version": "0.0.0", "support_contact": "help@hmwagner.com",
                        "status": "degraded", "status_message": ""}""",
                ),
        )

        composeTestRule.setContent {
            AppNav(windowSizeClass = compactWindowSizeClass)
        }

        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText("Maintenance").fetchSemanticsNodes().isNotEmpty()
        }
        composeTestRule.onNodeWithText("Down for scheduled maintenance.").assertExists()
        composeTestRule.onNodeWithText("Contact: help@hmwagner.com").assertExists()
        // Neither Home nor Login must have rendered underneath.
        composeTestRule.onAllNodesWithText("Ask about a machine").assertCountEquals(0)
        composeTestRule.onAllNodesWithText("Technician Manual Assistant").assertCountEquals(0)
    }

    @Test
    fun aSuccessfulAuthenticatedRequestDoesNotRedirectToLogin() {
        server.enqueue(configOkResponse())
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
            "expected exactly the fake login, AppNav's config fetch, its launch-time /me check, " +
                "plus one recentMachines() call",
            4,
            server.requestCount,
        )

        val loginNodes = composeTestRule.onAllNodesWithText("Technician Manual Assistant").fetchSemanticsNodes()
        assertTrue("a successful request must not trigger the session-expiry redirect", loginNodes.isEmpty())
        assertTrue(ApiClient.hasSession())
    }

    // No state, label, message, evidence, saved status, or selection from
    // one account may be visible after another account signs in. Drives a
    // real conversation open via UI clicks (not just setting
    // HomeSelectionViewModel fields directly), then forces a session expiry
    // and asserts that logging back in -- even as the SAME account, which is
    // the harder case since a stale label collision wouldn't be visible as an
    // obviously wrong account -- lands back on the machine list, not straight
    // into the old conversation. AppNav's sessionExpired handler must clear
    // HomeSelectionViewModel too -- it's Activity-scoped, not tied to the
    // HOME back-stack entry the way the NavHost-cleared ViewModels are, so
    // leaving it untouched would let a fresh login reopen the old
    // conversation.
    @Test
    fun sessionExpiryClearsTheSelectedConversationSoALaterLoginStartsClean() {
        server.enqueue(configOkResponse())
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

    // Routes.chat() must not interpolate the machine label directly
    // into the route string ("chat/$id?label=$label"). A "/" would split it
    // into extra path segments (breaking route matching entirely); "&" or
    // "%" would corrupt the query value; a real model label ("AJ/AX 100 &
    // Co. 50%-rated") could contain any of these. Uri.encode() on the write
    // side is deliberately paired with NO manual decode on the read side --
    // confirmed by reading androidx.navigation 2.9.8's own source
    // (NavDeepLink.kt's query-argument branch, backed by NavUri, a straight
    // typealias for android.net.Uri on this platform) that
    // Uri.getQueryParameters already returns a decoded value; adding a
    // second decode would corrupt any label containing a literal "%".
    @Test
    fun aMachineLabelWithReservedUriCharactersNavigatesAndDisplaysCorrectly() {
        val trickyLabel = "AJ/AX 100 & Co. 50%"
        server.enqueue(configOkResponse())
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
                .setBody(
                    """{"id": 42, "machine_id": 7, "machine_label": "$trickyLabel", "title": null, "started_at": "", "updated_at": ""}""",
                ),
        )
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

        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText(trickyLabel).fetchSemanticsNodes().isNotEmpty()
        }
        composeTestRule.onNodeWithText(trickyLabel).assertExists()
    }

    // A technician must always be able to sign out of THIS device, even if
    // the server can't be reached to revoke the session server-side.
    @Test
    fun logoutClearsTheLocalSessionEvenWhenTheServerIsUnreachable() {
        server.shutdown()

        runBlocking { ApiClient.logout() }

        assertFalse("logout must clear the local session even when the server call fails", ApiClient.hasSession())
    }

    // logout() must not await the network call before clearing local
    // state -- a dead/slow connection could otherwise leave the app looking
    // signed in for up to the full 90s read timeout after the tap. Local
    // state (including sessionExpired, which AppNav's redirect reacts to)
    // must flip immediately; the server call is best-effort only, afterward.
    @OptIn(DelicateCoroutinesApi::class)
    @Test
    fun logoutSignalsSessionExpiredWellBeforeASlowServerCallCompletes() {
        server.enqueue(MockResponse().setResponseCode(200).setBodyDelay(10, TimeUnit.SECONDS))

        GlobalScope.launch { ApiClient.logout() }

        // Polling a short deadline, not a fixed sleep -- proves this happens
        // fast, not merely "eventually" within a window a slow CI runner
        // could satisfy even if the network call were awaited first.
        val deadline = System.currentTimeMillis() + 1_000
        while (System.currentTimeMillis() < deadline && !ApiClient.sessionExpired.value) {
            Thread.sleep(10)
        }
        assertTrue(
            "sessionExpired must flip well before the 10s slow logout response arrives",
            ApiClient.sessionExpired.value,
        )
        assertFalse(ApiClient.hasSession())
    }
}
