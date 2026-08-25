package com.hmwagner.techmanual.ui.machines

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.CreateConversationRequest
import com.hmwagner.techmanual.network.MachineOut
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
                    _state.value = _state.value.copy(recent = resp.body().orEmpty())
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
        viewModelScope.launch {
            _state.value = _state.value.copy(refreshing = true, error = null)
            val q = _state.value.query
            try {
                if (q.isBlank()) {
                    val resp = ApiClient.service.recentMachines()
                    if (resp.isSuccessful) {
                        _state.value = _state.value.copy(recent = resp.body().orEmpty())
                    } else {
                        _state.value = _state.value.copy(error = "Couldn't refresh (code ${resp.code()}).")
                    }
                } else {
                    val resp = ApiClient.service.searchMachines(query = q)
                    if (resp.isSuccessful) {
                        _state.value = _state.value.copy(results = resp.body().orEmpty())
                    } else {
                        _state.value = _state.value.copy(error = "Couldn't refresh (code ${resp.code()}).")
                    }
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(error = "Can't reach the server. Check your connection.")
            } finally {
                _state.value = _state.value.copy(refreshing = false)
            }
        }
    }

    fun onQueryChange(q: String) {
        _state.value = _state.value.copy(query = q)
        search(q)
    }

    private fun search(q: String) {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, error = null)
            try {
                val resp = ApiClient.service.searchMachines(query = q)
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(loading = false, results = resp.body().orEmpty())
                } else {
                    _state.value = _state.value.copy(loading = false, error = "Search failed (code ${resp.code()}).")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(loading = false, error = "Can't reach the server. Check your connection.")
            }
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
                    ApiClient.service.touchMachine(machine.id)
                    _state.value = _state.value.copy(creatingConversation = false)
                    onCreated(conv.id, conv.machine_label)
                } else {
                    _state.value = _state.value.copy(creatingConversation = false, error = "Couldn't start a conversation (code ${resp.code()}).")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(creatingConversation = false, error = "Can't reach the server. Check your connection.")
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
                    _state.value = _state.value.copy(creatingConversation = false, error = "Couldn't start a conversation (code ${resp.code()}).")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(creatingConversation = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }
}
