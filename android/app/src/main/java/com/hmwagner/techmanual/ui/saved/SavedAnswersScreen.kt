package com.hmwagner.techmanual.ui.saved

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.hmwagner.techmanual.network.SavedAnswerOut
import com.hmwagner.techmanual.ui.common.LogoutAction

/**
 * Technician-facing list of every answer saved from ChatScreen's "Save"
 * link (GET /api/saved-answers) -- see SavedAnswersViewModel's doc comment
 * for why this exists. Tapping an entry reopens the conversation it came
 * from, the same navigation contract HistoryScreen uses.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SavedAnswersScreen(
    onConversationSelected: (Int, String?) -> Unit,
    onBack: () -> Unit,
    vm: SavedAnswersViewModel = viewModel(),
) {
    val state by vm.state.collectAsState()

    // Same reasoning as HistoryScreen's LaunchedEffect(Unit): viewModel() can
    // resolve back to a retained instance, so refresh on every composition
    // entry, not just construction.
    LaunchedEffect(Unit) { vm.refresh() }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Saved answers") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back to machines")
                    }
                },
                actions = { LogoutAction() },
            )
        },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding).padding(16.dp)) {
            if (state.error != null) {
                Text(
                    state.error!!,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(bottom = 8.dp).semantics { liveRegion = LiveRegionMode.Polite },
                )
            }

            if (state.loading && state.answers.isEmpty()) {
                CircularProgressIndicator(modifier = Modifier.padding(16.dp))
            } else {
                PullToRefreshBox(
                    isRefreshing = state.loading,
                    onRefresh = vm::refresh,
                    modifier = Modifier.fillMaxSize(),
                ) {
                    if (state.answers.isEmpty()) {
                        Text(
                            "No saved answers yet -- tap Save on an answer to keep it here.",
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    } else {
                        LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            items(state.answers, key = { it.answer.id }) { saved ->
                                SavedAnswerRow(
                                    saved,
                                    onClick = { onConversationSelected(saved.conversation_id, saved.machine_label) },
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SavedAnswerRow(saved: SavedAnswerOut, onClick: () -> Unit) {
    ListItem(
        headlineContent = { Text(saved.question ?: "Saved answer") },
        supportingContent = {
            Column {
                Text(saved.machine_label ?: "No machine selected", style = MaterialTheme.typography.labelMedium)
                Text(
                    previewText(saved.answer.content),
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        },
        modifier = Modifier.fillMaxWidth().clickable(onClick = onClick),
    )
}

/**
 * Collapses an answer's raw markdown-ish content (the same `_italic caveat_`
 * / `**bold**` / `- bullet` syntax ChatScreen's FormattedAnswer parses into
 * styled text) into a single plain-text line for this row's compact
 * two-line preview. Found live 2026-09-16: this row used to show the answer
 * unparsed, so a low-confidence caveat rendered as a literal
 * "_Low confidence: ..._" line with visible underscores instead of either
 * italic text or plain text -- FormattedAnswer's full multi-composable
 * rendering doesn't fit a compact list preview, so this strips the syntax
 * instead of reproducing that styling here.
 */
private fun previewText(raw: String): String =
    raw.lineSequence()
        .map { it.trim().removePrefix("- ").removePrefix("• ") }
        .filter { it.isNotEmpty() }
        .joinToString(" ")
        .replace(Regex("\\*\\*(.+?)\\*\\*"), "$1")
        .replace(Regex("(?<!\\w)_(.+?)_(?!\\w)"), "$1")
