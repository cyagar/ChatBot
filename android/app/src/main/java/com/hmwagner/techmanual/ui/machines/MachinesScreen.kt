package com.hmwagner.techmanual.ui.machines

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Star
import androidx.compose.material.icons.outlined.StarOutline
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.hmwagner.techmanual.network.MachineOut
import com.hmwagner.techmanual.ui.common.LogoutAction

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MachinesScreen(
    onMachineSelected: (Int, String?) -> Unit,
    onHistoryClick: (() -> Unit)? = null,
    onSavedAnswersClick: (() -> Unit)? = null,
    vm: MachinesViewModel = viewModel(),
) {
    val state by vm.state.collectAsState()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Ask about a machine") },
                actions = {
                    onSavedAnswersClick?.let { onClick ->
                        IconButton(onClick = onClick) {
                            Icon(Icons.Filled.Bookmark, contentDescription = "Saved answers")
                        }
                    }
                    onHistoryClick?.let { onClick ->
                        IconButton(onClick = onClick) {
                            Icon(Icons.Filled.History, contentDescription = "Conversation history")
                        }
                    }
                    LogoutAction()
                },
            )
        },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding).padding(16.dp)) {
            OutlinedTextField(
                value = state.query,
                onValueChange = vm::onQueryChange,
                label = { Text("Search manufacturer, model, or family") },
                leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null) },
                // Right where the technician is already looking while typing --
                // the results list below is easy to miss on a quick glance, and
                // a search against the real API takes long enough (debounce +
                // network round trip) that some visible "this is working" cue
                // matters (found live on-device 2026-09-16).
                trailingIcon = {
                    if (state.loading) {
                        CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
                    }
                },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )

            if (state.error != null) {
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 8.dp)) {
                    Text(
                        state.error!!,
                        color = MaterialTheme.colorScheme.error,
                        modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
                    )
                    if (state.query.isNotBlank()) {
                        TextButton(onClick = vm::retrySearch) { Text("Retry") }
                    }
                }
            }

            TextButton(
                onClick = { vm.startWithoutMachine(onMachineSelected) },
                enabled = !state.creatingConversation,
                modifier = Modifier.padding(top = 4.dp),
            ) {
                Text("Not sure which machine? Just ask -- I'll ask you to confirm it.")
            }

            val listToShow = if (state.query.isBlank()) state.recent else state.results
            val sectionTitle = if (state.query.isBlank()) "Recent & favorites" else "Results"

            Text(
                sectionTitle,
                style = MaterialTheme.typography.labelLarge,
                modifier = Modifier.padding(top = 16.dp, bottom = 4.dp),
            )

            if (!state.loading && listToShow.isEmpty()) {
                Text(
                    if (state.query.isBlank()) "No recent machines yet -- search above to get started."
                    else "No matching machines found.",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            // Scoped to the list itself, not the search field/buttons above --
            // refresh() re-runs whichever of recents/search is currently
            // showing (see the comment on MachinesViewModel.refresh()).
            PullToRefreshBox(
                isRefreshing = state.refreshing,
                onRefresh = vm::refresh,
                modifier = Modifier.weight(1f).fillMaxWidth(),
            ) {
                LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    items(listToShow, key = { it.id }) { machine ->
                        MachineRow(
                            machine,
                            enabled = !state.creatingConversation,
                            onClick = { vm.selectMachine(machine, onCreated = onMachineSelected) },
                            onToggleFavorite = { vm.toggleFavorite(machine) },
                        )
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MachineRow(
    machine: MachineOut,
    enabled: Boolean,
    onClick: () -> Unit,
    onToggleFavorite: () -> Unit,
) {
    ListItem(
        headlineContent = { Text("${machine.manufacturer} ${machine.model_name}") },
        supportingContent = {
            val family = machine.family
            val suffix = "${machine.document_count} manual(s)"
            Text(if (family != null) "$family • $suffix" else suffix)
        },
        // Always present (not just when favorited) -- otherwise there is no
        // way to favorite a machine that isn't already one. A tap here must
        // not also select the machine, so this is its own IconButton rather
        // than part of the row's clickable area.
        leadingContent = {
            IconButton(onClick = onToggleFavorite, enabled = enabled) {
                if (machine.is_favorite) {
                    Icon(Icons.Filled.Star, contentDescription = "Unfavorite")
                } else {
                    Icon(Icons.Outlined.StarOutline, contentDescription = "Favorite")
                }
            }
        },
        modifier = Modifier
            .fillMaxWidth()
            .clickable(enabled = enabled, onClick = onClick),
    )
}
