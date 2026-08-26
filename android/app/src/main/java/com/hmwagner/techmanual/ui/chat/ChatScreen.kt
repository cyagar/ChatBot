package com.hmwagner.techmanual.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import coil3.compose.AsyncImage
import com.hmwagner.techmanual.BuildConfig
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.CitationOut
import com.hmwagner.techmanual.network.MessageOut
import com.hmwagner.techmanual.ui.common.LogoutAction
import com.hmwagner.techmanual.ui.theme.warningColor

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(conversationId: Int, machineLabel: String?, onBack: (() -> Unit)? = null) {
    // Explicit `key`, not just the default class-name-based one: viewModel()
    // without a key scopes the ViewModelStore lookup to the class name alone
    // (via the LocalViewModelStoreOwner -- here, the Activity, since
    // TwoPaneHome has no NavBackStackEntry to scope it per-route the way
    // SinglePaneHome's NavHost does). Wrapping this call in
    // `key(selectedId) { ... }` (see TwoPaneHome in AppNav.kt) changes the
    // Compose slot but does NOT reset that ViewModelStore lookup, so without
    // this explicit key, switching from one conversation straight to another
    // in the two-pane detail pane silently returned the *same* cached
    // ChatViewModel instance -- its `init` never re-ran, so it kept showing
    // the previous conversation's stale state (found via live tablet
    // testing 2026-08-25, the same session the History screen was added --
    // instrumented with temporary Log.d calls in ChatScreen/TwoPaneHome to
    // confirm recomposition WAS happening with the new id while no new
    // network call ever fired).
    val vm: ChatViewModel = viewModel(key = "chat-$conversationId", factory = ChatViewModel.Factory(conversationId))
    val state by vm.state.collectAsState()
    val listState = rememberLazyListState()

    LaunchedEffect(state.messages.size, state.pendingEcho) {
        val lastIndex = state.messages.size + (if (state.pendingEcho != null) 1 else 0)
        if (lastIndex > 0) listState.animateScrollToItem(lastIndex - 1)
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("Chat")
                        Text(
                            machineLabel ?: "No machine selected",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                },
                actions = {
                    // Null in two-pane layout (AppNav.kt's TwoPaneHome): the
                    // machine list is already visible in the left pane, so
                    // there's nothing for this button to navigate back to.
                    onBack?.let { back ->
                        IconButton(onClick = back) {
                            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back to choose a machine")
                        }
                    }
                    LogoutAction()
                },
            )
        },
        bottomBar = { Composer(state, vm) },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            // The centered spinner only covers the true first load (nothing
            // to show yet); a subsequent pull-to-refresh instead relies on
            // PullToRefreshBox's own top indicator below, so the existing
            // messages stay visible underneath while it reloads.
            if (state.loadingHistory && state.messages.isEmpty()) {
                CircularProgressIndicator(Modifier.align(Alignment.Center))
            } else {
                PullToRefreshBox(
                    isRefreshing = state.loadingHistory,
                    onRefresh = vm::refresh,
                    modifier = Modifier.fillMaxSize(),
                ) {
                    LazyColumn(
                        state = listState,
                        modifier = Modifier.fillMaxSize(),
                        contentPadding = androidx.compose.foundation.layout.PaddingValues(12.dp),
                        verticalArrangement = Arrangement.spacedBy(10.dp),
                    ) {
                        items(state.messages, key = { it.id }) { msg ->
                            MessageBubble(msg, onCitationClick = vm::openCitation, onRetry = { vm.retry(msg.id) },
                                onClarifyingSelect = vm::selectClarifyingMachine,
                                onFeedback = { rating -> vm.submitFeedback(msg.id, rating) },
                                onSave = { vm.saveAnswer(msg.id) },
                                feedbackGiven = state.feedbackGiven[msg.id],
                                saved = state.savedMessageIds.contains(msg.id))
                        }
                        state.pendingEcho?.let { echo ->
                            item(key = "pending-${echo.id}") {
                                PendingUserBubble(
                                    echo.content,
                                    sending = state.sending,
                                    uncertain = state.pendingEchoUncertain,
                                    stillProcessing = state.pendingEchoStillProcessing,
                                    onRetry = vm::retryPendingSend,
                                )
                            }
                        }
                    }
                }
            }

            if (state.error != null) {
                Card(
                    modifier = Modifier.align(Alignment.BottomCenter).padding(12.dp).fillMaxWidth(),
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer),
                ) {
                    Text(
                        state.error!!,
                        modifier = Modifier.padding(12.dp).semantics { liveRegion = LiveRegionMode.Polite },
                        color = MaterialTheme.colorScheme.onErrorContainer,
                    )
                }
            }
        }
    }

    if (state.evidenceLoading || state.evidence != null || state.evidenceError != null) {
        EvidenceSheet(state, onDismiss = vm::dismissEvidence, onRetry = vm::retryEvidence)
    }
}

@Composable
private fun PendingUserBubble(
    text: String,
    sending: Boolean,
    uncertain: Boolean,
    stillProcessing: Boolean,
    onRetry: () -> Unit,
) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
        Card(
            modifier = Modifier.widthIn(max = 480.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer),
        ) {
            Column(Modifier.padding(12.dp)) {
                Text(text)
                if (sending) {
                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 6.dp)) {
                        CircularProgressIndicator(Modifier.padding(end = 6.dp).size(14.dp), strokeWidth = 2.dp)
                        Text("Searching approved manuals…", style = MaterialTheme.typography.labelSmall)
                    }
                } else if (stillProcessing) {
                    // P0A-2: the server DEFINITELY has this exact question
                    // (a reload found its own persisted user turn with no
                    // reply after it yet) and is still working on it, or
                    // died before finishing -- deliberately NOT the same
                    // "Connection lost" warning styling as the genuinely
                    // uncertain case below, since nothing here is actually
                    // lost or unknown. Retry re-checks safely: it reuses
                    // this question's idempotency key, so it can never
                    // create a duplicate turn even if the answer finishes
                    // between now and the tap landing.
                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 6.dp)) {
                        CircularProgressIndicator(Modifier.padding(end = 6.dp).size(14.dp), strokeWidth = 2.dp)
                        Text(
                            "Still generating an answer for this…",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onPrimaryContainer,
                        )
                    }
                    TextButton(onClick = onRetry, modifier = Modifier.padding(top = 2.dp)) { Text("Check again") }
                } else if (uncertain) {
                    // Connection dropped mid-send and we genuinely don't
                    // know the outcome -- say so instead of just silently
                    // dropping the spinner, which used to look identical to
                    // a normal already-sent message. Retry reuses this same
                    // question's idempotency key, so it's always safe to
                    // tap: it can never create a duplicate turn even if the
                    // original attempt actually landed.
                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 6.dp)) {
                        Icon(
                            Icons.Filled.Warning,
                            contentDescription = null,
                            tint = warningColor,
                            modifier = Modifier.size(14.dp),
                        )
                        Text(
                            "Connection lost -- unknown if this was received",
                            style = MaterialTheme.typography.labelSmall,
                            color = warningColor,
                            modifier = Modifier.padding(start = 6.dp),
                        )
                    }
                    TextButton(onClick = onRetry, modifier = Modifier.padding(top = 2.dp)) { Text("Retry") }
                }
            }
        }
    }
}

private val NUMBERED_LINE = Regex("""^(\d+)\.\s(.*)""")

/**
 * The backend emits a small, fixed markdown subset for assistant answers --
 * claims as "- " bullets, then (if any steps) a literal "**Steps:**" header
 * line followed by "N. " numbered lines (see parse_and_validate in
 * base.py) -- never general Markdown. A plain Text(msg.content) showed that
 * literally: raw "- " dashes and literal "**" asterisks around "Steps:",
 * which is what looked bad. This renders that exact fixed shape instead of
 * pulling in a full Markdown library for three line patterns.
 */
@Composable
private fun FormattedAnswer(content: String) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        for (line in content.split("\n")) {
            val numbered = NUMBERED_LINE.matchEntire(line)
            when {
                line.isBlank() -> Spacer(Modifier.height(4.dp))
                line.startsWith("**") && line.endsWith("**") && line.length > 4 -> Text(
                    line.removePrefix("**").removeSuffix("**"),
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.padding(top = 4.dp),
                )
                line.startsWith("- ") -> Row {
                    Text("•", modifier = Modifier.padding(end = 8.dp))
                    Text(line.removePrefix("- "), modifier = Modifier.weight(1f))
                }
                numbered != null -> Row {
                    Text("${numbered.groupValues[1]}.", modifier = Modifier.padding(end = 8.dp))
                    Text(numbered.groupValues[2], modifier = Modifier.weight(1f))
                }
                else -> Text(line)
            }
        }
    }
}

@Composable
private fun MessageBubble(
    msg: MessageOut,
    onCitationClick: (CitationOut) -> Unit,
    onRetry: () -> Unit,
    onClarifyingSelect: (Int) -> Unit,
    onFeedback: (String) -> Unit,
    onSave: () -> Unit,
    feedbackGiven: String?,
    saved: Boolean,
) {
    val isUser = msg.role == "user"
    // A no-answer response is the honest, by-design outcome when retrieval
    // can't ground a claim in an approved manual (plan section 10.2: "Clear
    // no-answer ... states"). It's a common outcome, not a bug -- give it its
    // own muted treatment so it reads as an intentional safety behavior
    // rather than a broken app.
    val isNoAnswer = msg.role == "assistant" && msg.is_no_answer && !msg.is_clarifying_question
    Row(Modifier.fillMaxWidth(), horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start) {
        Card(
            modifier = Modifier.widthIn(max = 560.dp),
            colors = CardDefaults.cardColors(
                containerColor = when {
                    isUser -> MaterialTheme.colorScheme.primaryContainer
                    isNoAnswer -> MaterialTheme.colorScheme.surfaceContainerHighest
                    else -> MaterialTheme.colorScheme.surfaceVariant
                },
            ),
        ) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {

                if (msg.safety_warnings.isNotEmpty()) {
                    msg.safety_warnings.forEach { warning ->
                        Row(verticalAlignment = Alignment.Top) {
                            Icon(Icons.Filled.Warning, contentDescription = null, tint = warningColor)
                            Text(warning, color = warningColor, modifier = Modifier.padding(start = 6.dp))
                        }
                    }
                }

                if (isNoAnswer) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Filled.Info, contentDescription = null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
                        Text(
                            "No verified answer from the approved manuals",
                            style = MaterialTheme.typography.labelLarge,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(start = 6.dp),
                        )
                    }
                }

                FormattedAnswer(msg.content)

                if (msg.answer_status == "failed") {
                    TextButton(onClick = onRetry) { Text("Retry") }
                }

                msg.conflict_note?.let { note ->
                    // Same "icon + explicit label, not just color" treatment
                    // as safety_warnings above -- this used to be color-only
                    // (amber text, no icon, no label), the exact "color
                    // -independent status" gap plan section 6 calls out, for
                    // a revision-conflict notice that section 2 lists as a
                    // must-not-regress invariant.
                    Row(verticalAlignment = Alignment.Top) {
                        Icon(Icons.Filled.Warning, contentDescription = null, tint = warningColor, modifier = Modifier.size(18.dp))
                        Text(
                            "Revision conflict: $note",
                            style = MaterialTheme.typography.bodySmall,
                            color = warningColor,
                            modifier = Modifier.padding(start = 6.dp),
                        )
                    }
                }

                if (msg.clarifying_options.isNotEmpty()) {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        msg.clarifying_options.forEach { option ->
                            AssistChip(onClick = { onClarifyingSelect(option.id) }, label = { Text(option.label) })
                        }
                    }
                }

                if (msg.citations.isNotEmpty()) {
                    Text("Sources", style = MaterialTheme.typography.labelMedium)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        msg.citations.forEachIndexed { i, citation ->
                            AssistChip(
                                onClick = { onCitationClick(citation) },
                                label = {
                                    // Override only this Text's own semantics
                                    // (not the chip's -- see android/README.md's
                                    // accessibility note) so a screen reader
                                    // announces a full sentence instead of reading the
                                    // compact "[1] p.5" glyphs literally, while
                                    // the chip's own click/Button-role semantics
                                    // (wired by AssistChip itself via `onClick`)
                                    // are untouched.
                                    Text(
                                        "[${i + 1}] p.${citation.page_number ?: "?"}",
                                        modifier = Modifier.semantics {
                                            contentDescription = buildString {
                                                append("Citation ${i + 1}")
                                                citation.page_number?.let { append(", page $it") }
                                                citation.title?.let { append(", $it") }
                                            }
                                        },
                                    )
                                },
                            )
                        }
                    }
                }

                if (msg.role == "assistant" && !msg.is_clarifying_question) {
                    // 8.dp, not the tighter 4.dp this used to be: measured live
                    // on the Tab A9+ via `adb shell uiautomator dump` that 4dp
                    // put only a 4dp gap between the actual clickable bounds of
                    // adjacent buttons (each already meets the 48dp touch-target
                    // *height* minimum on its own) -- half of the ~8dp Material
                    // Design recommends between adjacent targets, and these two
                    // are opposite-meaning actions (Helpful vs Incorrect), which
                    // is exactly the case plan section 13.5's gloves concern is
                    // about: a mis-tap here doesn't just miss, it records the
                    // wrong feedback.
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                        if (feedbackGiven == null) {
                            TextButton(onClick = { onFeedback("helpful") }) { Text("Helpful") }
                            TextButton(onClick = { onFeedback("incorrect") }) { Text("Incorrect") }
                        } else {
                            Text(
                                if (feedbackGiven == "helpful") "Marked helpful" else "Marked incorrect",
                                style = MaterialTheme.typography.labelMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                modifier = Modifier.padding(start = 12.dp, end = 8.dp),
                            )
                        }
                        TextButton(onClick = onSave, enabled = !saved) { Text(if (saved) "Saved" else "Save") }
                    }
                }
            }
        }
    }
}

@Composable
private fun Composer(state: ChatUiState, vm: ChatViewModel) {
    val maxLen = 2000
    Row(
        // A plain Row (unlike NavigationBar/BottomAppBar) does not consume
        // system-bar insets on its own. Without this, the composer -- and
        // critically the send button -- render underneath the gesture
        // navigation bar on edge-to-edge devices: touches there get eaten by
        // the system's edge-swipe gesture handling instead of reaching the
        // button. Confirmed live on a physical Tab A9+ (SM-X210, Android 15,
        // gesture nav) before this fix -- the button was unreachable.
        modifier = Modifier.fillMaxWidth().navigationBarsPadding().imePadding().padding(12.dp),
        verticalAlignment = Alignment.Bottom,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        OutlinedTextField(
            value = state.composerText,
            onValueChange = { if (it.length <= maxLen) vm.onComposerChange(it) },
            modifier = Modifier.weight(1f),
            placeholder = { Text("Ask a question about the selected machine…") },
            supportingText = { Text("${state.composerText.length} / $maxLen") },
        )
        IconButton(onClick = vm::send, enabled = !state.sending && state.composerText.isNotBlank()) {
            Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send")
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun EvidenceSheet(state: ChatUiState, onDismiss: () -> Unit, onRetry: () -> Unit) {
    val sheetState = rememberModalBottomSheetState()
    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = sheetState) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (state.evidenceLoading) {
                CircularProgressIndicator()
            } else if (state.evidenceError != null) {
                // P0A-4: this used to be unreachable -- a non-2xx or a
                // thrown exception left both evidence and this null, and
                // the sheet is only shown for (evidenceLoading || evidence
                // != null), so the request just silently failed with no
                // visible error and no way to retry.
                Text(state.evidenceError, color = MaterialTheme.colorScheme.error)
                TextButton(onClick = onRetry) { Text("Retry") }
            } else if (state.evidence != null) {
                val evidence = state.evidence
                Text(evidence.title ?: evidence.filename, style = MaterialTheme.typography.titleMedium)
                Text(
                    listOfNotNull(
                        evidence.revision?.let { "Revision $it" },
                        evidence.page_number?.let { "Page $it" },
                        evidence.section_heading,
                    ).joinToString("  •  "),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (evidence.has_page_image && state.evidenceDocumentId != null) {
                    // The page image IS the evidence here -- showing the
                    // parsed/extracted text underneath it too is redundant
                    // (and occasionally a worse read than the real page,
                    // e.g. after imperfect OCR). Only fall back to the text
                    // card below when there's no image to show instead.
                    val url = "${BuildConfig.BASE_URL}api/manuals/${state.evidenceDocumentId}/pages/${evidence.page_number}/image"
                    AsyncImage(
                        model = url,
                        imageLoader = ApiClient.imageLoader,
                        contentDescription = "Manual page ${evidence.page_number}",
                        contentScale = ContentScale.FillWidth,
                        modifier = Modifier.fillMaxWidth(),
                    )
                } else {
                    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)) {
                        Text(evidence.content, modifier = Modifier.padding(12.dp))
                    }
                }
            } else {
                Text("Evidence not available.")
            }
        }
    }
}
