package com.hmwagner.techmanual.ui.machines

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Star
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.hmwagner.techmanual.network.MachineOut

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MachinesScreen(onMachineSelected: (Int, String?) -> Unit, vm: MachinesViewModel = viewModel()) {
    val state by vm.state.collectAsState()

    Scaffold(
        topBar = { TopAppBar(title = { Text("Ask about a machine") }) },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding).padding(16.dp)) {
            OutlinedTextField(
                value = state.query,
                onValueChange = vm::onQueryChange,
                label = { Text("Search manufacturer, model, or family") },
                leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )

            if (state.error != null) {
                Text(
                    state.error!!,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(top = 8.dp).semantics { liveRegion = LiveRegionMode.Polite },
                )
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

            if (state.loading) {
                CircularProgressIndicator(modifier = Modifier.padding(16.dp))
            }

            if (!state.loading && listToShow.isEmpty()) {
                Text(
                    if (state.query.isBlank()) "No recent machines yet -- search above to get started."
                    else "No matching machines found.",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            LazyColumn(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                items(listToShow, key = { it.id }) { machine ->
                    MachineRow(machine, enabled = !state.creatingConversation) {
                        vm.selectMachine(machine, onCreated = onMachineSelected)
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MachineRow(machine: MachineOut, enabled: Boolean, onClick: () -> Unit) {
    ListItem(
        headlineContent = { Text("${machine.manufacturer} ${machine.model_name}") },
        supportingContent = {
            val family = machine.family
            val suffix = "${machine.document_count} manual(s)"
            Text(if (family != null) "$family • $suffix" else suffix)
        },
        leadingContent = if (machine.is_favorite) {
            { Icon(Icons.Filled.Star, contentDescription = "Favorite") }
        } else null,
        modifier = Modifier
            .fillMaxWidth()
            .clickable(enabled = enabled, onClick = onClick),
    )
}
