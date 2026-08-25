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
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import coil3.compose.AsyncImage
import com.hmwagner.techmanual.BuildConfig
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.CitationOut
import com.hmwagner.techmanual.network.MessageOut
import com.hmwagner.techmanual.ui.theme.warningColor

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(conversationId: Int, machineLabel: String?, onBack: (() -> Unit)? = null) {
    val vm: ChatViewModel = viewModel(factory = ChatViewModel.Factory(conversationId))
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
                },
            )
        },
        bottomBar = { Composer(state, vm) },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            if (state.loadingHistory) {
                CircularProgressIndicator(Modifier.align(Alignment.Center))
            } else {
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
                                onRetry = vm::retryPendingSend,
                            )
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

    if (state.evidenceLoading || state.evidence != null) {
        EvidenceSheet(state, onDismiss = vm::dismissEvidence)
    }
}

@Composable
private fun PendingUserBubble(text: String, sending: Boolean, uncertain: Boolean, onRetry: () -> Unit) {
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
                } else if (uncertain) {
                    // Connection dropped mid-send (or the server says this
                    // exact question is already being processed) and we
                    // genuinely don't know the outcome -- say so instead of
                    // just silently dropping the spinner, which used to look
                    // identical to a normal already-sent message. Retry
                    // reuses this same question's idempotency key, so it's
                    // always safe to tap: it can never create a duplicate
                    // turn even if the original attempt actually landed.
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

                Text(msg.content)

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
                                    // accessibility note) so TalkBack announces
                                    // a full sentence instead of reading the
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
private fun EvidenceSheet(state: ChatUiState, onDismiss: () -> Unit) {
    val sheetState = rememberModalBottomSheetState()
    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = sheetState) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (state.evidenceLoading) {
                CircularProgressIndicator()
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
                Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)) {
                    Text(evidence.content, modifier = Modifier.padding(12.dp))
                }
                if (evidence.has_page_image && state.evidenceDocumentId != null) {
                    val url = "${BuildConfig.BASE_URL}api/manuals/${state.evidenceDocumentId}/pages/${evidence.page_number}/image"
                    AsyncImage(
                        model = url,
                        imageLoader = ApiClient.imageLoader,
                        contentDescription = "Manual page ${evidence.page_number}",
                        contentScale = ContentScale.FillWidth,
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
            } else {
                Text("Evidence not available.")
            }
        }
    }
}
