package com.hmwagner.techmanual.ui.nav

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Chat
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.windowsizeclass.ExperimentalMaterial3WindowSizeClassApi
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.material3.windowsizeclass.WindowWidthSizeClass
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.ui.chat.ChatScreen
import com.hmwagner.techmanual.ui.history.HistoryScreen
import com.hmwagner.techmanual.ui.login.LoginScreen
import com.hmwagner.techmanual.ui.machines.MachinesScreen

/**
 * Holds the two-pane/single-pane selected conversation. A plain ViewModel,
 * scoped to MainActivity's ViewModelStore, which the framework keeps alive
 * across a configuration change (rotation, entering/leaving split-screen)
 * regardless of which composable branch -- TwoPaneHome or SinglePaneHome --
 * happens to be mounted when the change lands. Confirmed via on-device
 * logging (2026-08-25, Tab A9+) that this selection state itself survives
 * rotation correctly either way (this ViewModel, or an earlier
 * rememberSaveable attempt) -- the bug that actually made rotation drop the
 * conversation lived in `SinglePaneHome` reusing a stale `NavController`
 * across branch switches, not in how this selection was held. See the
 * `key(...)` wrapping `SinglePaneHome` in `HomeContent` below for the real
 * fix and its explanation.
 *
 * Must be at least package-visible: the default `ViewModelProvider` factory
 * instantiates via reflection and throws `IllegalAccessException` on a
 * `private` class. `internal` (public at the JVM level) is sufficient and
 * was confirmed live on-device -- no need for `public`.
 */
internal class HomeSelectionViewModel : ViewModel() {
    var selectedId by mutableStateOf<Int?>(null)
    var selectedLabel by mutableStateOf<String?>(null)
}

private object Routes {
    const val LOGIN = "login"
    const val HOME = "home"
    const val MACHINES = "machines"
    const val HISTORY = "history"
    const val CHAT = "chat/{conversationId}?label={label}"
    fun chat(conversationId: Int, label: String?) = "chat/$conversationId?label=${label ?: ""}"
}

@OptIn(ExperimentalMaterial3WindowSizeClassApi::class)
@Composable
fun AppNav(windowSizeClass: WindowSizeClass) {
    val navController = rememberNavController()
    val startDestination = remember { if (ApiClient.hasSession()) Routes.HOME else Routes.LOGIN }
    val sessionExpired by ApiClient.sessionExpired.collectAsState()
    // Obtained here, above NavHost, so it resolves against the ambient
    // LocalViewModelStoreOwner at THIS point in composition -- the hosting
    // Activity itself (see MainActivity.setContent -> AppNav(...)) -- rather
    // than a NavBackStackEntry's own ViewModelStore.
    val selection: HomeSelectionViewModel = viewModel()

    LaunchedEffect(sessionExpired) {
        if (sessionExpired) {
            navController.navigate(Routes.LOGIN) {
                popUpTo(0) { inclusive = true }
            }
            ApiClient.onSessionExpiredHandled()
        }
    }

    NavHost(navController = navController, startDestination = startDestination) {
        composable(Routes.LOGIN) {
            LoginScreen(onLoggedIn = {
                navController.navigate(Routes.HOME) {
                    popUpTo(Routes.LOGIN) { inclusive = true }
                }
            })
        }
        composable(Routes.HOME) {
            HomeContent(selection = selection, isExpanded = windowSizeClass.widthSizeClass == WindowWidthSizeClass.Expanded)
        }
    }
}

/**
 * Owns the selected-conversation state above the one/two-pane branch so it
 * survives switching between them -- rotating the tablet (or dragging
 * into/out of split-screen) recreates the Activity, and whichever branch
 * wasn't active at that moment would otherwise never see the selection its
 * sibling made. Hoisting it here, above both, means each branch reads and
 * writes the same source of truth regardless of which one is on screen.
 */
@Composable
private fun HomeContent(selection: HomeSelectionViewModel, isExpanded: Boolean) {
    val onSelect: (Int, String?) -> Unit = { id, label -> selection.selectedId = id; selection.selectedLabel = label }

    if (isExpanded) {
        TwoPaneHome(selectedId = selection.selectedId, selectedLabel = selection.selectedLabel, onSelect = onSelect)
    } else {
        // Keyed on the selection: confirmed via on-device logging
        // (2026-08-25, Tab A9+) that the selection state itself survives
        // switching branches fine, but SinglePaneHome's *own* NavController
        // doesn't -- Compose reuses the same NavController instance (and
        // its own remembered/restored back stack) across a branch
        // removal+reinsertion within one composition, so `startDestination`
        // below is silently ignored on every re-entry after the first: the
        // controller already has a "current destination" (whatever it was
        // showing the last time this branch was active) and NavHost only
        // consults `startDestination` when there's no existing one. Keying
        // on the selection forces a genuinely fresh NavController whenever
        // the selection differs from what it was the last time this branch
        // was shown, without discarding in-branch nav state (e.g. mid-draft
        // composer text) when the selection hasn't actually changed.
        key(selection.selectedId ?: -1) {
            SinglePaneHome(
                selectedId = selection.selectedId,
                selectedLabel = selection.selectedLabel,
                onSelect = onSelect,
                onBack = { selection.selectedId = null; selection.selectedLabel = null },
            )
        }
    }
}

/**
 * The existing (pre-adaptive) Machines -> Chat flow, with its own back
 * stack. Starts straight on Chat if a conversation is already selected --
 * e.g. rotating out of the two-pane layout -- instead of dropping back to
 * the machine list.
 */
@Composable
private fun SinglePaneHome(
    selectedId: Int?,
    selectedLabel: String?,
    onSelect: (Int, String?) -> Unit,
    onBack: () -> Unit,
) {
    val innerNav = rememberNavController()
    val startDestination = if (selectedId != null) Routes.chat(selectedId, selectedLabel) else Routes.MACHINES
    NavHost(navController = innerNav, startDestination = startDestination) {
        composable(Routes.MACHINES) {
            MachinesScreen(
                onMachineSelected = { conversationId, label ->
                    onSelect(conversationId, label)
                    innerNav.navigate(Routes.chat(conversationId, label))
                },
                onHistoryClick = { innerNav.navigate(Routes.HISTORY) },
            )
        }
        composable(Routes.HISTORY) {
            HistoryScreen(
                onConversationSelected = { conversationId, label ->
                    // onSelect writes selection.selectedId, which changes the
                    // key(...) wrapping SinglePaneHome in HomeContent -- this
                    // whole composable (innerNav included) is torn down and
                    // rebuilt with a fresh NavController whose
                    // startDestination is Chat directly, on the next
                    // recomposition. That rebuild, not the popUpTo below, is
                    // what actually makes Chat's back button land on the
                    // machine list rather than back through History: the new
                    // controller's back stack never contained History or
                    // Machines to begin with. The navigate() call here (on
                    // the soon-to-be-discarded old innerNav) just avoids a
                    // stale frame before that rebuild lands; popUpTo is kept
                    // for the same "undo one step, not re-walk History"
                    // intent in case a future change makes this controller
                    // longer-lived.
                    onSelect(conversationId, label)
                    innerNav.navigate(Routes.chat(conversationId, label)) {
                        popUpTo(Routes.MACHINES)
                    }
                },
                onBack = { innerNav.popBackStack() },
            )
        }
        composable(
            route = Routes.CHAT,
            arguments = listOf(
                navArgument("conversationId") { type = NavType.IntType },
                navArgument("label") { type = NavType.StringType; defaultValue = "" },
            ),
        ) { backStackEntry ->
            val conversationId = backStackEntry.arguments?.getInt("conversationId") ?: return@composable
            val label = backStackEntry.arguments?.getString("label")?.takeIf { it.isNotBlank() }
            ChatScreen(
                conversationId = conversationId,
                machineLabel = label,
                onBack = {
                    onBack()
                    innerNav.popBackStack()
                },
            )
        }
    }
}

/**
 * Wide-window (Expanded) layout: the machine list stays visible in a fixed
 * left pane while the active conversation renders beside it in the right
 * pane, per plan section 6 ("machine/history navigation beside the active
 * conversation where it improves speed"). Selection is plain hoisted state,
 * not a nav route -- there's no "back" to go to when both panes are already
 * on screen (see ChatScreen's nullable onBack).
 */
@Composable
private fun TwoPaneHome(selectedId: Int?, selectedLabel: String?, onSelect: (Int, String?) -> Unit) {
    // Plain hoisted toggle, not a nav route, matching `selectedId` above --
    // there's no "back" affordance needed in a fixed pane, just a switch
    // between what it shows.
    var showHistory by remember { mutableStateOf(false) }
    Row(Modifier.fillMaxSize()) {
        Box(Modifier.width(360.dp).fillMaxHeight()) {
            if (showHistory) {
                HistoryScreen(
                    onConversationSelected = { conversationId, label ->
                        onSelect(conversationId, label)
                        showHistory = false
                    },
                    onBack = { showHistory = false },
                )
            } else {
                MachinesScreen(onMachineSelected = onSelect, onHistoryClick = { showHistory = true })
            }
        }
        HorizontalDivider(modifier = Modifier.fillMaxHeight().width(1.dp))
        Box(Modifier.fillMaxHeight()) {
            if (selectedId == null) {
                EmptyDetailPane()
            } else {
                // key(selectedId) alone is NOT what prevents ChatScreen from
                // reusing the previous conversation's ChatViewModel -- found
                // via live testing (2026-08-25) that it wasn't preventing
                // that at all: viewModel() without an explicit key looks
                // itself up by class name in the Activity's ViewModelStore,
                // untouched by this recomposition-scoping key(). The actual
                // fix is the explicit `key = "chat-$conversationId"` passed
                // to viewModel() inside ChatScreen itself -- see the comment
                // there. This key() is kept anyway since it still forces a
                // clean recomposition of everything else in the subtree
                // (LazyListState, scroll position, etc.) on conversation
                // switches, which is harmless and arguably still desired.
                key(selectedId) {
                    ChatScreen(conversationId = selectedId, machineLabel = selectedLabel, onBack = null)
                }
            }
        }
    }
}

@Composable
private fun EmptyDetailPane() {
    Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.surface) {
        Column(
            modifier = Modifier.fillMaxSize().padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Icon(
                Icons.AutoMirrored.Filled.Chat,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                "Select a machine to start a conversation",
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 8.dp),
            )
        }
    }
}
