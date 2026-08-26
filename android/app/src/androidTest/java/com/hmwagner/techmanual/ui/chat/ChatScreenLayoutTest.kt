package com.hmwagner.techmanual.ui.chat

import android.content.Context
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.dp
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.hmwagner.techmanual.network.ApiClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * P0A-5: MessageBubble's clarifying-options, citation-chip, and
 * Helpful/Incorrect/Save rows used to be plain `Row`s, which don't wrap --
 * enough items (or long enough labels) at a narrow phone width or large
 * font scale render past the card's edge with no way to reach the
 * off-screen ones. Fixed with `FlowRow`. This drives one representative
 * case (clarifying options, the one most likely to carry several
 * long/localized-length labels at once) through a real Compose layout pass
 * constrained to 360dp -- the narrowest width the plan's device matrix
 * names.
 *
 * The actual pre-fix failure mode (found by dumping the real semantics
 * tree on-device, 2026-08-26) is NOT chips positioned past the container's
 * right edge -- a plain `Row` given a bounded max-width constraint from its
 * `Box(width = 360.dp)` ancestor doesn't let total children width exceed
 * that bound either. Instead, once the first long chip consumes nearly all
 * 360dp, every subsequent chip in the same `Row` gets measured with
 * essentially zero remaining width and collapses to a literal
 * zero-size placement (`Rect.fromLTRB(504.0, 209.0, 504.0, ...)` --
 * left == right) rather than rendering at all: still present in the
 * semantics tree (so `onNodeWithText` finds it), completely invisible and
 * untappable in practice. So the real assertion is that every chip has a
 * genuinely positive width, not that its right edge stays in bounds --
 * checking only the latter would trivially "pass" on a collapsed,
 * zero-width chip.
 *
 * Deliberately not a JVM/Robolectric test: this needs a real Compose
 * layout/measurement pass, which only an instrumented test (or a device)
 * can give an honest answer about.
 */
@RunWith(AndroidJUnit4::class)
class ChatScreenLayoutTest {

    @get:Rule
    val composeTestRule = createComposeRule()

    private lateinit var server: MockWebServer

    private val longOptionLabels = listOf(
        "Bunn Axiom XL Commercial Coffee Brewer Series 3000",
        "Hobart HL600 Planetary Mixer Extended Warranty Edition",
        "Hoshizaki KM-515MAH Ice Machine Air-Cooled Undercounter",
        "Middleby Marshall PS640G Conveyor Oven Double Stack",
    )

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        val context = ApplicationProvider.getApplicationContext<Context>()
        ApiClient.initForTest(context, server.url("/").toString())
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun clarifyingOptionChipsWrapInsteadOfCollapsingAt360dpWidth() {
        val optionsJson = longOptionLabels.mapIndexed { i, label ->
            """{"id": ${i + 1}, "label": "$label"}"""
        }.joinToString(",", prefix = "[", postfix = "]")
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """[{
                        "id": 1, "role": "assistant", "content": "Which machine do you mean?",
                        "is_clarifying_question": true, "clarifying_options": $optionsJson,
                        "created_at": "2026-08-24T00:00:00Z"
                    }]""",
                ),
        )

        composeTestRule.setContent {
            Box(Modifier.width(360.dp).fillMaxHeight().testTag("narrowContainer")) {
                ChatScreen(conversationId = 1, machineLabel = "Test Machine")
            }
        }

        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText(longOptionLabels.last()).fetchSemanticsNodes().isNotEmpty()
        }

        val containerBounds = composeTestRule.onNodeWithTag("narrowContainer").fetchSemanticsNode().boundsInRoot

        longOptionLabels.forEach { label ->
            val bounds = composeTestRule.onNodeWithText(label).fetchSemanticsNode().boundsInRoot
            assertTrue(
                "chip \"$label\" collapsed to zero width ($bounds) -- a plain Row squeezes every " +
                    "chip after the first into whatever space is left, which runs out immediately " +
                    "once one long label already fills the container",
                bounds.width > 10f,
            )
            assertTrue(
                "chip \"$label\" ($bounds) must stay within the 360dp container ($containerBounds)",
                bounds.right <= containerBounds.right + 1f, // +1px slack for rounding
            )
        }
    }
}
