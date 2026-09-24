package com.hmwagner.techmanual.ui.machines

import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.CreateConversationRequest
import com.hmwagner.techmanual.network.MachineOut
import com.hmwagner.techmanual.network.describeError
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

data class MachinesUiState(
    val query: String = "",
    val recent: List<MachineOut> = emptyList(),
    val results: List<MachineOut> = emptyList(),
    val loading: Boolean = false,
    val refreshing: Boolean = false,
    val creatingConversation: Boolean = false,
    val error: String? = null,
)

class MachinesViewModel : ViewModel() {
    private val _state = MutableStateFlow(MachinesUiState())
    val state: StateFlow<MachinesUiState> = _state

    init {
        loadRecent()
    }

    fun loadRecent() {
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.recentMachines()
                if (resp.isSuccessful) {
                    // Clears any error left over from an earlier action (e.g.
                    // a failed favorite toggle) -- this ViewModel can outlive
                    // several screen visits (MachinesScreen has no
                    // LaunchedEffect-driven re-fetch the way History/
                    // SavedAnswers do), so a stale banner could otherwise sit
                    // on screen indefinitely after whatever caused it had
                    // long since succeeded on retry. Found live on-device
                    // (2026-09-16): favoriting a machine after an earlier,
                    // unrelated failed request still showed "Can't reach the
                    // server" even though the favorite call itself succeeded.
                    _state.value = _state.value.copy(recent = resp.body().orEmpty(), error = null)
                }
            } catch (_: Exception) {
                // Recents are a convenience, not critical -- fail quietly and
                // let the technician search instead.
            }
        }
    }

    /**
     * Pull-to-refresh: re-runs whichever list is currently on screen (recents
     * when the search box is blank, otherwise the active search) rather than
     * always reloading recents -- pulling to refresh mid-search should
     * refresh the search results, not silently discard them for the recent
     * list. Unlike the quiet-failure `loadRecent()` above, a pull is a
     * deliberate user action, so a failure here does surface an error.
     */
    fun refresh() {
        // Shares searchJob with onQueryChange() below: cancelling
        // any pending/in-flight search before a pull-to-refresh avoids a
        // wasted duplicate request for the same query, and -- the direction
        // that actually matters -- lets a keystroke landing DURING a refresh
        // cancel it in turn via the same searchJob?.cancel() there, instead
        // of a slower refresh response landing after a newer search result
        // and overwriting it.
        searchJob?.cancel()
        searchJob = viewModelScope.launch {
            _state.value = _state.value.copy(refreshing = true, error = null)
            val q = _state.value.query
            try {
                if (q.isBlank()) {
                    val resp = ApiClient.service.recentMachines()
                    if (resp.isSuccessful) {
                        _state.value = _state.value.copy(recent = resp.body().orEmpty())
                    } else {
                        _state.value = _state.value.copy(error = resp.describeError("Couldn't refresh"))
                    }
                } else {
                    val resp = ApiClient.service.searchMachines(query = q)
                    // Same staleness guard as search() below -- the query
                    // could have changed while this request was in flight.
                    if (_state.value.query != q) return@launch
                    if (resp.isSuccessful) {
                        _state.value = _state.value.copy(results = resp.body().orEmpty())
                    } else {
                        _state.value = _state.value.copy(error = resp.describeError("Couldn't refresh"))
                    }
                }
            } catch (e: CancellationException) {
                throw e // structured concurrency: never swallow a real cancellation
            } catch (_: Exception) {
                _state.value = _state.value.copy(error = "Can't reach the server. Check your connection.")
            } finally {
                _state.value = _state.value.copy(refreshing = false)
            }
        }
    }

    // The one in-flight (or debouncing) search job, if any -- search() must
    // not launch a brand new, uncancelled coroutine on every keystroke, or
    // an older, slower response could land AFTER a newer one and silently
    // overwrite its results with stale data. Cancelling the previous job
    // before starting a new one closes that at the source; the query-match
    // check inside search() below is a second, independent guard for the
    // rare case a response is already in flight by the time cancel() lands.
    private var searchJob: Job? = null

    private companion object {
        // Long enough to skip the request entirely for someone still
        // actively typing, short enough not to feel unresponsive. Kept short
        // deliberately: the round trip itself already costs ~500ms against
        // the real (Neon-backed) API, confirmed live on-device 2026-09-16 --
        // stacking a long debounce on top of that made "search as you type"
        // read as "nothing happens" for someone glancing down after typing
        // just a couple characters.
        const val SEARCH_DEBOUNCE_MS = 150L
        const val TAG = "MachinesViewModel"
    }

    fun onQueryChange(q: String) {
        _state.value = _state.value.copy(query = q, error = null)
        searchJob?.cancel()
        if (q.isBlank()) {
            // Matches existing behavior: a blank query shows `recent`, not
            // `results` (see MachinesScreen), so there's nothing to search.
            _state.value = _state.value.copy(loading = false)
            return
        }
        // Immediate, not deferred until after the debounce delay below --
        // the technician should see something happened right away, even
        // though the actual request is intentionally delayed.
        _state.value = _state.value.copy(loading = true)
        searchJob = viewModelScope.launch {
            try {
                delay(SEARCH_DEBOUNCE_MS)
                search(q)
            } finally {
                // If this job was cancelled before search() ran (e.g. by
                // refresh() or a newer keystroke) it never got to clear
                // `loading` itself. Only clear it here if a newer keystroke
                // hasn't already claimed `loading` for its own query.
                if (_state.value.query == q) {
                    _state.value = _state.value.copy(loading = false)
                }
            }
        }
    }

    /**
     * Re-runs the search for the current query after a failure -- surfaced
     * as a "Retry" button next to the error banner. A search failure (e.g.
     * the dead-pooled-connection SocketException confirmed live on-device
     * 2026-09-16) otherwise leaves the technician stuck with no way back to
     * results short of editing the query again.
     */
    fun retrySearch() {
        val q = _state.value.query
        if (q.isBlank()) return
        searchJob?.cancel()
        _state.value = _state.value.copy(loading = true, error = null)
        searchJob = viewModelScope.launch {
            try {
                search(q)
            } finally {
                if (_state.value.query == q) {
                    _state.value = _state.value.copy(loading = false)
                }
            }
        }
    }

    private suspend fun search(q: String) {
        try {
            val resp = ApiClient.service.searchMachines(query = q)
            // The technician may have kept typing while this request was in
            // flight -- searchJob's own cancellation is the first line of
            // defense, but this check is what actually prevents a response
            // that was ALREADY in flight when a newer keystroke landed from
            // overwriting that newer query's results.
            if (_state.value.query != q) return
            if (resp.isSuccessful) {
                _state.value = _state.value.copy(loading = false, results = resp.body().orEmpty())
            } else {
                _state.value = _state.value.copy(loading = false, error = resp.describeError("Search failed"))
            }
        } catch (e: CancellationException) {
            throw e // structured concurrency: never swallow a real cancellation
        } catch (_: Exception) {
            if (_state.value.query != q) return
            _state.value = _state.value.copy(loading = false, error = "Can't reach the server. Check your connection.")
        }
    }

    /** Creates a conversation for [machine] and invokes [onCreated] with the new conversation id and label. */
    fun selectMachine(machine: MachineOut, onCreated: (Int, String?) -> Unit) {
        viewModelScope.launch {
            _state.value = _state.value.copy(creatingConversation = true, error = null)
            try {
                val resp = ApiClient.service.createConversation(CreateConversationRequest(machine.id))
                if (resp.isSuccessful) {
                    val conv = resp.body()!!
                    _state.value = _state.value.copy(creatingConversation = false)
                    onCreated(conv.id, conv.machine_label)
                    // Fired best-effort, after onCreated, not awaited before
                    // it -- awaiting it here would mean a thrown exception
                    // (a network exception between the two calls) makes the
                    // whole function's catch block report total failure
                    // even though the conversation was already committed
                    // server-side, so it would never open AND a retry could
                    // create a second, empty conversation for the same
                    // machine. touchMachine is a recency/favorites
                    // convenience, not part of the conversation itself -- its
                    // failure must never block navigation to an already
                    // -created conversation or turn it into a reported error.
                    touchMachineBestEffort(machine.id)
                } else {
                    _state.value = _state.value.copy(creatingConversation = false, error = resp.describeError("Couldn't start a conversation"))
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(creatingConversation = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }

    /**
     * Toggles favorite state for [machine] in both the recent and search
     * lists it might currently appear in -- optimistic (flips immediately,
     * before the network call resolves) since this is a low-stakes,
     * frequently-tapped action where waiting on a round trip would feel
     * laggy; reverted in both lists on failure so the UI never drifts from
     * server truth.
     */
    fun toggleFavorite(machine: MachineOut) {
        val newValue = !machine.is_favorite
        fun apply(list: List<MachineOut>, value: Boolean) =
            list.map { if (it.id == machine.id) it.copy(is_favorite = value) else it }
        _state.value = _state.value.copy(
            recent = apply(_state.value.recent, newValue),
            results = apply(_state.value.results, newValue),
            error = null,
        )
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.setFavorite(machine.id, newValue)
                if (!resp.isSuccessful) {
                    _state.value = _state.value.copy(
                        recent = apply(_state.value.recent, machine.is_favorite),
                        results = apply(_state.value.results, machine.is_favorite),
                        error = resp.describeError("Couldn't update favorite"),
                    )
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(
                    recent = apply(_state.value.recent, machine.is_favorite),
                    results = apply(_state.value.results, machine.is_favorite),
                    error = "Can't reach the server. Check your connection.",
                )
            }
        }
    }

    private fun touchMachineBestEffort(machineId: Int) {
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.touchMachine(machineId)
                if (!resp.isSuccessful) {
                    Log.w(TAG, "touchMachine($machineId) failed: HTTP ${resp.code()}")
                }
            } catch (e: Exception) {
                // Recency/favorites tracking only -- never blocks or reports
                // failure on the conversation this was attached to, which
                // has already opened by the time this runs. Logged (not
                // silently swallowed) so it's independently observable.
                Log.w(TAG, "touchMachine($machineId) failed", e)
            }
        }
    }

    /**
     * Starts a conversation with no machine pre-selected. Naming the machine
     * in the question itself exercises the server's clarify-instead-of-guess
     * path (routes_chat.py's ask_question/_resolve_machine_mention) -- the
     * app must never infer a machine change silently, so this is the only
     * other entry point besides picking one from the list.
     */
    fun startWithoutMachine(onCreated: (Int, String?) -> Unit) {
        viewModelScope.launch {
            _state.value = _state.value.copy(creatingConversation = true, error = null)
            try {
                val resp = ApiClient.service.createConversation(CreateConversationRequest(machine_id = null))
                if (resp.isSuccessful) {
                    val conv = resp.body()!!
                    _state.value = _state.value.copy(creatingConversation = false)
                    onCreated(conv.id, null)
                } else {
                    _state.value = _state.value.copy(creatingConversation = false, error = resp.describeError("Couldn't start a conversation"))
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(creatingConversation = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }
}
