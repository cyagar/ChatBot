package com.hmwagner.techmanual.ui.nav

import androidx.compose.material3.windowsizeclass.ExperimentalMaterial3WindowSizeClassApi
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
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
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * GET /api/config's latest_version/update_url drive a non-blocking "update
 * available" banner (see AppNav.kt's UpdateAvailableBanner) -- distinct from
 * minimum_supported_version's full-screen lockout, which
 * AppNavSessionExpiryTest.maintenanceModeBlocksTheAppBeforeHomeOrLoginRenders'
 * sibling test already covers. Runs on-device for the same reason that file
 * does: PersistentCookieJar's encryption needs the real Android Keystore.
 */
@OptIn(ExperimentalMaterial3WindowSizeClassApi::class)
@RunWith(AndroidJUnit4::class)
class AppNavUpdateNoticeTest {

    @get:Rule
    val composeTestRule = createComposeRule()

    private lateinit var server: MockWebServer

    private val compactWindowSizeClass = WindowSizeClass.calculateFromSize(DpSize(400.dp, 800.dp))

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()

        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        ApiClient.initForTest(context, server.url("/").toString())
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
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun configResponse(latestVersion: String, updateUrl: String) = MockResponse()
        .setResponseCode(200)
        .setHeader("Content-Type", "application/json")
        .setBody(
            """{"maintenance_mode": false, "maintenance_message": "", "minimum_supported_version": "0.0.0",
                "support_contact": "", "status": "ok", "status_message": "",
                "latest_version": "$latestVersion", "update_url": "$updateUrl"}""",
        )

    @Test
    fun aNewerLatestVersionShowsADismissibleNonBlockingBanner() {
        server.enqueue(configResponse("9.9.9", "https://example.com/latest.apk"))
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"id": 1, "email": "tech.demo@hmwagner.com", "role": "technician"}"""),
        )
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody("[]"),
        ) // recentMachines()

        composeTestRule.setContent {
            AppNav(windowSizeClass = compactWindowSizeClass)
        }

        // Home itself must still render underneath -- this is a notice, not
        // a lockout the way BlockingConfigScreen is.
        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText("Ask about a machine").fetchSemanticsNodes().isNotEmpty()
        }
        composeTestRule.onNodeWithText("Ask about a machine").assertExists()
        composeTestRule.onNodeWithText("Version 9.9.9 is available.").assertExists()

        composeTestRule.onNodeWithText("Dismiss").performClick()
        composeTestRule.onAllNodesWithText("Version 9.9.9 is available.").assertCountEquals(0)
    }

    @Test
    fun blankLatestVersionShowsNoBanner() {
        server.enqueue(configResponse("", ""))
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

        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText("Ask about a machine").fetchSemanticsNodes().isNotEmpty()
        }
        composeTestRule.onAllNodesWithText("Update", substring = false).assertCountEquals(0)
    }
}
