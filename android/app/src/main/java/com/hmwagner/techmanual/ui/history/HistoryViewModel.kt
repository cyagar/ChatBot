package com.hmwagner.techmanual.ui.history

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.ConversationOut
import com.hmwagner.techmanual.network.describeError
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

data class HistoryUiState(
    val conversations: List<ConversationOut> = emptyList(),
    val loading: Boolean = true,
    val loadingMore: Boolean = false,
    val nextCursor: String? = null,
    val error: String? = null,
)

class HistoryViewModel : ViewModel() {
    private val _state = MutableStateFlow(HistoryUiState())
    val state: StateFlow<HistoryUiState> = _state

    // No init{} refresh() here -- HistoryScreen's LaunchedEffect(Unit) covers
    // the first load too (it must fire on every composition entry anyway,
    // not just genuine construction, to handle a retained ViewModel
    // instance -- see the comment there). Calling refresh() from both would
    // just fire two redundant requests on every first mount.
    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, error = null)
            try {
                val resp = ApiClient.service.listConversations()
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(
                        conversations = resp.body().orEmpty(),
                        loading = false,
                        nextCursor = resp.headers()["X-Next-Cursor"],
                    )
                } else {
                    _state.value = _state.value.copy(loading = false, error = resp.describeError("Couldn't load history"))
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(loading = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }

    // Passes a cursor to GET /conversations so a technician with more than
    // one page of history (limit=20) can see anything older than that.
    // Appends onto the existing list rather than replacing it, the
    // opposite of refresh().
    fun loadMore() {
        val cursor = _state.value.nextCursor ?: return
        if (_state.value.loadingMore) return
        viewModelScope.launch {
            _state.value = _state.value.copy(loadingMore = true, error = null)
            try {
                val resp = ApiClient.service.listConversations(cursor = cursor)
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(
                        conversations = _state.value.conversations + resp.body().orEmpty(),
                        loadingMore = false,
                        nextCursor = resp.headers()["X-Next-Cursor"],
                    )
                } else {
                    _state.value = _state.value.copy(loadingMore = false, error = resp.describeError("Couldn't load more history"))
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(loadingMore = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }
}
