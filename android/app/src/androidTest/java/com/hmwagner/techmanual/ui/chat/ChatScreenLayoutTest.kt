package com.hmwagner.techmanual.ui.chat

import android.content.Context
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.Density
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
 * MessageBubble's clarifying-options, citation-chip, and
 * Helpful/Incorrect/Save rows must use `FlowRow`, not plain `Row`s, which
 * don't wrap -- enough items (or long enough labels) at a narrow phone
 * width or large font scale would render past the card's edge with no way
 * to reach the off-screen ones.
 *
 * The failure mode a plain `Row` produces is NOT chips positioned past the
 * container's right edge -- a plain `Row` given a bounded max-width
 * constraint from its `Box(width = ...)` ancestor doesn't let total
 * children width exceed that bound either. Instead, once the first long
 * chip consumes nearly all the available width, every subsequent chip in
 * the same `Row` gets measured with essentially zero remaining width and
 * collapses to a literal zero-size placement (left == right) rather than
 * rendering at all: still present in the semantics tree (so
 * `onNodeWithText` finds it), completely invisible and untappable in
 * practice. So the real assertion is that every chip has a genuinely
 * positive width, not that its right edge stays in bounds -- checking only
 * the latter would trivially "pass" on a collapsed, zero-width chip.
 *
 * Deliberately not a JVM/Robolectric test: this needs a real Compose
 * layout/measurement pass, which only an instrumented test (or a device)
 * can give an honest answer about.
 *
 * Exercises a width x font-scale matrix: 360/411dp (phone), and 700/900dp
 * (Medium/Expanded tablet width classes, using the same Box-constraint
 * technique as the phone widths since a tablet's own screen doesn't reach a
 * narrow width naturally) each at both the default and a 200% font scale.
 * The `CompositionLocalProvider(LocalDensity provides Density(...,
 * fontScale = X))` override was confirmed on-device to actually reach
 * AssistChip's rendered text -- chip height grows from 48dp (the Material3
 * minimum touch target, which dominates at default scale) to 56dp at a 2x
 * override on an otherwise identical chip, real measured growth, not a
 * no-op.
 *
 * NOT covered by this file: portrait/landscape and split-screen are not
 * exercised as literal device rotations or multi-window states here --
 * MessageBubble has no orientation- or window-mode-conditional code
 * (confirmed by inspection), so the width matrix above is taken as
 * covering the same layout paths a real rotation or split-screen would
 * hit, but that is a reasoned equivalence, not a device observation of
 * rotation/multi-window itself. Keyboard-open is genuinely different in
 * kind (a height, not width, change) and isn't simulated here at all --
 * see the README for what's known about `imePadding()` there instead of a
 * test that fakes an IME. A human accessibility-service listening session
 * and a real-phone run are both still open -- neither is possible from
 * this environment (no phone hardware here, and TalkBack must never be
 * enabled programmatically on this tablet).
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

    // (width in dp, font scale) -- 360/411 are the plan's named phone
    // widths, 700/900 stand in for the Medium/Expanded tablet window-size
    // classes (Material3's WindowWidthSizeClass breakpoints are 600dp and
    // 840dp), each at the default and a 200% font scale.
    private val layoutMatrix = listOf(
        360 to 1f, 360 to 2f,
        411 to 1f, 411 to 2f,
        700 to 1f, 700 to 2f,
        900 to 1f, 900 to 2f,
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

    // ChatViewModel fetches the conversation before its messages, so every
    // ChatScreen scenario must queue this ahead of the messages response.
    private fun conversationResponse() = MockResponse().setResponseCode(200)
        .setHeader("Content-Type", "application/json")
        .setBody("""{"id": 1, "machine_id": null, "machine_label": "Test Machine", "title": null, "started_at": "", "updated_at": ""}""")

    @Test
    fun clarifyingOptionChipsWrapAcrossTheSupportedWidthAndFontScaleMatrix() {
        val optionsJson = longOptionLabels.mapIndexed { i, label ->
            """{"id": ${i + 1}, "label": "$label"}"""
        }.joinToString(",", prefix = "[", postfix = "]")
        server.enqueue(conversationResponse())
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

        val widthDp = mutableStateOf(360)
        val fontScale = mutableStateOf(1f)

        composeTestRule.setContent {
            val base = LocalDensity.current
            CompositionLocalProvider(LocalDensity provides Density(base.density, fontScale = fontScale.value)) {
                Box(Modifier.width(widthDp.value.dp).fillMaxHeight().testTag("narrowContainer")) {
                    ChatScreen(conversationId = 1, machineLabel = "Test Machine")
                }
            }
        }

        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText(longOptionLabels.last()).fetchSemanticsNodes().isNotEmpty()
        }

        layoutMatrix.forEach { (w, scale) ->
            composeTestRule.runOnIdle {
                widthDp.value = w
                fontScale.value = scale
            }
            composeTestRule.waitForIdle()

            val containerBounds = composeTestRule.onNodeWithTag("narrowContainer").fetchSemanticsNode().boundsInRoot
            longOptionLabels.forEach { label ->
                val bounds = composeTestRule.onNodeWithText(label).fetchSemanticsNode().boundsInRoot
                assertTrue(
                    "[${w}dp @ ${scale}x] chip \"$label\" collapsed to zero width ($bounds) -- a plain Row " +
                        "squeezes every chip after the first into whatever space is left, which runs out " +
                        "immediately once one long label already fills the container",
                    bounds.width > 10f,
                )
                assertTrue(
                    "[${w}dp @ ${scale}x] chip \"$label\" ($bounds) must stay within the container ($containerBounds)",
                    bounds.right <= containerBounds.right + 1f, // +1px slack for rounding
                )
            }
        }
    }

    @Test
    fun citationChipsWrapWithManyShortChipsAcrossTheWidthAndFontScaleMatrix() {
        // Citation chip labels are always short ("[N] p.NN" -- see
        // MessageBubble's citation row; the citation's title/filename only
        // ever reaches the contentDescription, not the visible label), so
        // the overflow risk here is chip COUNT, not label length. Eight
        // citations is more than any real answer has produced in this demo
        // but is a plausible worst case for a heavily-cited answer.
        val citationsJson = (1..8).joinToString(",", prefix = "[", postfix = "]") { n ->
            """{"chunk_id": $n, "document_id": 1, "filename": "manual.pdf", "page_number": $n, "excerpt": "x"}"""
        }
        server.enqueue(conversationResponse())
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """[{
                        "id": 1, "role": "assistant", "content": "Replace the part as shown.",
                        "is_clarifying_question": false, "citations": $citationsJson,
                        "created_at": "2026-08-24T00:00:00Z"
                    }]""",
                ),
        )

        val widthDp = mutableStateOf(360)
        val fontScale = mutableStateOf(1f)

        composeTestRule.setContent {
            val base = LocalDensity.current
            CompositionLocalProvider(LocalDensity provides Density(base.density, fontScale = fontScale.value)) {
                Box(Modifier.width(widthDp.value.dp).fillMaxHeight().testTag("narrowContainer")) {
                    ChatScreen(conversationId = 1, machineLabel = "Test Machine")
                }
            }
        }

        composeTestRule.waitUntil(timeoutMillis = 5_000) {
            composeTestRule.onAllNodesWithText("[8] p.8").fetchSemanticsNodes().isNotEmpty()
        }

        layoutMatrix.forEach { (w, scale) ->
            composeTestRule.runOnIdle {
                widthDp.value = w
                fontScale.value = scale
            }
            composeTestRule.waitForIdle()

            val containerBounds = composeTestRule.onNodeWithTag("narrowContainer").fetchSemanticsNode().boundsInRoot
            (1..8).forEach { n ->
                val label = "[$n] p.$n"
                val bounds = composeTestRule.onNodeWithText(label).fetchSemanticsNode().boundsInRoot
                assertTrue(
                    "[${w}dp @ ${scale}x] citation chip \"$label\" collapsed to zero width ($bounds)",
                    bounds.width > 10f,
                )
                assertTrue(
                    "[${w}dp @ ${scale}x] citation chip \"$label\" ($bounds) must stay within the container ($containerBounds)",
                    bounds.right <= containerBounds.right + 1f,
                )
            }
        }
    }
}
