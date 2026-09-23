package com.hmwagner.techmanual.ui.nav

import android.net.Uri
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
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.windowsizeclass.ExperimentalMaterial3WindowSizeClassApi
import androidx.compose.material3.windowsizeclass.WindowSizeClass
import androidx.compose.material3.windowsizeclass.WindowWidthSizeClass
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
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
import com.hmwagner.techmanual.ui.saved.SavedAnswersScreen
import kotlinx.coroutines.withTimeoutOrNull

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
    const val SAVED_ANSWERS = "saved_answers"
    const val CHAT = "chat/{conversationId}?label={label}"
    // P0A-4: a raw machine label interpolated directly into the route could
    // break navigation entirely (a "/" splits it into extra path segments)
    // or corrupt the query value ("&", "?", "%"). android.net.Uri.encode is
    // used deliberately, not java.net.URLEncoder: Navigation's own route
    // matching (NavDeepLink -> NavUri, a straight typealias for
    // android.net.Uri on this platform) decodes query args via
    // Uri.getQueryParameters, which is the exact inverse of Uri.encode --
    // URLEncoder's form-encoding (space -> "+") is not.
    fun chat(conversationId: Int, label: String?) = "chat/$conversationId?label=${Uri.encode(label ?: "")}"
}

/**
 * P0A-1: a stored session cookie used to be treated as proof of a valid
 * signed-in user -- `hasSession()` only checks that *something* is saved,
 * not that the server still honors it (expired, revoked, or the account was
 * disabled since the cookie was written). `Checking` gates the very first
 * frame on a real `/me` call so a stale/invalid cookie lands on Login before
 * any account-scoped screen ever renders, instead of flashing Home and then
 * bouncing back via the 401 interceptor.
 */
private enum class LaunchSessionState { Checking, SignedIn, SignedOut }

@OptIn(ExperimentalMaterial3WindowSizeClassApi::class)
@Composable
fun AppNav(windowSizeClass: WindowSizeClass) {
    val navController = rememberNavController()
    val sessionExpired by ApiClient.sessionExpired.collectAsState()
    // Obtained here, above NavHost, so it resolves against the ambient
    // LocalViewModelStoreOwner at THIS point in composition -- the hosting
    // Activity itself (see MainActivity.setContent -> AppNav(...)) -- rather
    // than a NavBackStackEntry's own ViewModelStore.
    val selection: HomeSelectionViewModel = viewModel()

    var launchState by remember { mutableStateOf(LaunchSessionState.Checking) }
    LaunchedEffect(Unit) {
        launchState = if (!ApiClient.hasSession()) {
            LaunchSessionState.SignedOut
        } else {
            // Bounded well below the client's real 15s connect / 90s read
            // timeouts (ApiClient.kt) -- those are sized for a slow real
            // answer, not for how long a technician should stare at an
            // unlabeled spinner on cold launch. A timeout here fails open
            // the same way an outright connection exception does, below;
            // the real /me call is left to finish in the background and its
            // result is simply discarded.
            withTimeoutOrNull(5_000) {
                try {
                    val resp = ApiClient.service.me()
                    if (resp.isSuccessful) {
                        LaunchSessionState.SignedIn
                    } else if (resp.code() == 401) {
                        // authExpiryInterceptor already cleared the cookie and
                        // set sessionExpired for a 401 here; consuming it now
                        // avoids a redundant navigate() once Home/Login mount.
                        ApiClient.clearSession()
                        ApiClient.onSessionExpiredHandled()
                        LaunchSessionState.SignedOut
                    } else {
                        // P1-17 (external review, 2026-09-21): a temporary
                        // 500/503/429 here used to be treated identically to
                        // a 401 -- an outage during cold launch signed a
                        // technician out of a perfectly valid session. Only
                        // 401 (get_current_user's own "session expired or
                        // invalid" / "user no longer exists" / "disabled"
                        // rejections -- see app/auth/deps.py) is proof the
                        // session itself is bad; anything else is a server
                        // hiccup and must fail open, same as the connection
                        // exception below.
                        LaunchSessionState.SignedIn
                    }
                } catch (_: Exception) {
                    // Couldn't reach the server to validate -- fail open on a
                    // cached session rather than locking a technician out of
                    // the whole app while merely offline. Every real action
                    // still requires connectivity (plan: "Internet is
                    // required for AI answers"), so this only affects
                    // whether Home renders while offline, not whether stale
                    // data is trusted for anything that matters.
                    LaunchSessionState.SignedIn
                }
            } ?: LaunchSessionState.SignedIn
        }
    }

    // A deliberate logout() shares this exact flag/handling with a 401
    // session expiry (see ApiClient.sessionExpired's doc comment) -- both
    // must drop every account-scoped screen the same way. HomeSelectionViewModel
    // is Activity-scoped (not tied to the HOME back-stack entry the way the
    // Machines/History/Chat ViewModels are), so popUpTo(0) below does NOT
    // clear it on its own -- without HomeContent's own DisposableEffect (see
    // below) actually doing that, a later login as a different account could
    // reopen the prior account's selected conversation id/label (P0A-1).
    // This effect deliberately does NOT clear selection itself: found live
    // on the Tab A9+ (2026-08-26), via a genuinely reproducible instrumented
    // -test failure invisible to any JVM test, that clearing
    // selection.selectedId here -- reactively, from code that runs
    // independently of whether Home has actually left composition yet --
    // changes the `key(selection.selectedId ?: -1)` wrapping SinglePaneHome
    // while Home might still be transiently composed, recomposing a
    // brand-new MachinesScreen/MachinesViewModel (and firing its own
    // recentMachines() call) in the gap before this navigate() call's own
    // backstack change actually removes Home from the tree. That stray,
    // then-cancelled request desynced a test's MockWebServer response queue
    // -- but the same race is real against a live server too, just
    // harmless-looking there (an extra request that loses its race with a
    // real backend). Reordering navigate() before the clear did NOT fix
    // this (confirmed by rerunning the same instrumented test) -- Compose
    // batches snapshot-state writes made without an intervening suspension
    // into the same recomposition pass regardless of source order, so
    // whichever runs first in the recomposer's own (unspecified) scope
    // -processing order still wins. The only reliable fix is to make the
    // clear happen as an actual consequence of Home leaving composition,
    // not a reactive side effect racing against it.
    //
    // Guarded on launchState != Checking: the launch-time /me call above
    // runs through the same authExpiryInterceptor as every other request, so
    // a 401 there also flips this flag -- without the guard, this effect
    // could call navController.navigate() before NavHost (below) has even
    // been composed, which throws (no NavGraph set yet). The /me branch
    // above already handles that case directly (clearSession +
    // onSessionExpiredHandled), so this effect has nothing left to do while
    // still Checking.
    LaunchedEffect(sessionExpired, launchState) {
        if (sessionExpired && launchState != LaunchSessionState.Checking) {
            // Does NOT clear selection.selectedId/selectedLabel directly --
            // see HomeContent's own DisposableEffect below for why, and for
            // where that clearing now actually happens.
            navController.navigate(Routes.LOGIN) {
                popUpTo(0) { inclusive = true }
            }
            ApiClient.onSessionExpiredHandled()
        }
    }

    when (launchState) {
        LaunchSessionState.Checking -> LaunchChecking()
        else -> {
            val startDestination = if (launchState == LaunchSessionState.SignedIn) Routes.HOME else Routes.LOGIN
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
    }
}

@Composable
private fun LaunchChecking() {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
            CircularProgressIndicator()
            Text("Checking your session…", color = MaterialTheme.colorScheme.onSurfaceVariant)
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

    // Clears the selection as a genuine CONSEQUENCE of Home leaving
    // composition (logout/session-expiry navigates to Login with
    // popUpTo(0), which removes Home entirely) -- not as a reactive side
    // effect racing against that removal (see the long comment on AppNav's
    // sessionExpired LaunchedEffect for why that races and fails on a real
    // device, P0A-1 found 2026-08-26). onDispose only runs once Home is
    // actually gone, so there's no window left for `key(selection.selectedId
    // ?: -1)` below to see a changed value and recompose a fresh
    // SinglePaneHome/MachinesViewModel while Home is still technically
    // alive.
    DisposableEffect(Unit) {
        onDispose {
            selection.selectedId = null
            selection.selectedLabel = null
        }
    }

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
                onSavedAnswersClick = { innerNav.navigate(Routes.SAVED_ANSWERS) },
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
        composable(Routes.SAVED_ANSWERS) {
            SavedAnswersScreen(
                // Same rebuild-on-selection reasoning as HistoryScreen above.
                onConversationSelected = { conversationId, label ->
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
private enum class LeftPane { Machines, History, SavedAnswers }

@Composable
private fun TwoPaneHome(selectedId: Int?, selectedLabel: String?, onSelect: (Int, String?) -> Unit) {
    // Plain hoisted toggle, not a nav route, matching `selectedId` above --
    // there's no "back" affordance needed in a fixed pane, just a switch
    // between what it shows.
    var leftPane by remember { mutableStateOf(LeftPane.Machines) }
    Row(Modifier.fillMaxSize()) {
        Box(Modifier.width(360.dp).fillMaxHeight()) {
            when (leftPane) {
                LeftPane.History -> HistoryScreen(
                    onConversationSelected = { conversationId, label ->
                        onSelect(conversationId, label)
                        leftPane = LeftPane.Machines
                    },
                    onBack = { leftPane = LeftPane.Machines },
                )
                LeftPane.SavedAnswers -> SavedAnswersScreen(
                    onConversationSelected = { conversationId, label ->
                        onSelect(conversationId, label)
                        leftPane = LeftPane.Machines
                    },
                    onBack = { leftPane = LeftPane.Machines },
                )
                LeftPane.Machines -> MachinesScreen(
                    onMachineSelected = onSelect,
                    onHistoryClick = { leftPane = LeftPane.History },
                    onSavedAnswersClick = { leftPane = LeftPane.SavedAnswers },
                )
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
