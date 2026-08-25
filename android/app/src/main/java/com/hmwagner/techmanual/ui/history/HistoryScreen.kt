package com.hmwagner.techmanual.ui.history

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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.hmwagner.techmanual.network.ConversationOut

/**
 * Past conversations across all machines, most recently updated first (the
 * same GET /conversations the backend already exposed for P1-3 -- this is
 * the Android UI for it, which didn't exist yet). Tapping one calls
 * `onConversationSelected` with its id/machine_label directly -- no new
 * conversation is created, unlike picking a machine from MachinesScreen.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HistoryScreen(
    onConversationSelected: (Int, String?) -> Unit,
    onBack: () -> Unit,
    vm: HistoryViewModel = viewModel(),
) {
    val state by vm.state.collectAsState()

    // viewModel() can resolve back to a retained instance (e.g. TwoPaneHome
    // toggling this composable in and out of the same fixed pane, or
    // navigating back to this route in SinglePaneHome), in which case
    // `init` won't fire again. Refreshing on every composition entry, not
    // just construction, keeps the list current regardless of instance
    // identity -- cheap given how small/rare this list is.
    LaunchedEffect(Unit) { vm.refresh() }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("History") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back to machines")
                    }
                },
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

            if (state.loading) {
                CircularProgressIndicator(modifier = Modifier.padding(16.dp))
            } else if (state.conversations.isEmpty()) {
                Text(
                    "No past conversations yet -- ask a question to start one.",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            } else {
                LazyColumn(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    items(state.conversations, key = { it.id }) { conv ->
                        ConversationRow(conv, onClick = { onConversationSelected(conv.id, conv.machine_label) })
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ConversationRow(conv: ConversationOut, onClick: () -> Unit) {
    ListItem(
        headlineContent = { Text(conv.title ?: "New conversation") },
        supportingContent = { Text(conv.machine_label ?: "No machine selected") },
        trailingContent = {
            Text(
                // Backend timestamps are "YYYY-MM-DD HH:MM:SS" -- trim to the
                // minute, which is all a "recently updated" list needs.
                conv.updated_at.take(16),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        },
        modifier = Modifier.fillMaxWidth().clickable(onClick = onClick),
    )
}
