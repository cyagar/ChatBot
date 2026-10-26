package com.hmwagner.techmanual.ui.history

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.ConversationOut
import com.hmwagner.techmanual.network.describeError
import kotlinx.coroutines.CancellationException
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

    // Bumped by every refresh(); loadMore() captures whatever generation was
    // current when it started. A response that lands after a NEWER refresh()
    // has already started is discarded -- otherwise a pull-to-refresh racing
    // a pagination load (e.g. a scroll-triggered loadMore still in flight
    // when the technician pulls to refresh) could let the slower one
    // overwrite the other's result with a stale list, or append page 2 onto
    // a list refresh() had already replaced out from under it.
    private var loadGeneration = 0

    // No init{} refresh() here -- HistoryScreen's LaunchedEffect(Unit) covers
    // the first load too (it must fire on every composition entry anyway,
    // not just genuine construction, to handle a retained ViewModel
    // instance -- see the comment there). Calling refresh() from both would
    // just fire two redundant requests on every first mount.
    fun refresh() {
        val generation = ++loadGeneration
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, error = null)
            try {
                val resp = ApiClient.service.listConversations()
                if (generation != loadGeneration) return@launch
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(
                        conversations = resp.body().orEmpty(),
                        loading = false,
                        nextCursor = resp.headers()["X-Next-Cursor"],
                    )
                } else {
                    _state.value = _state.value.copy(loading = false, error = resp.describeError("Couldn't load history"))
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                if (generation != loadGeneration) return@launch
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
        val generation = loadGeneration
        viewModelScope.launch {
            _state.value = _state.value.copy(loadingMore = true, error = null)
            try {
                val resp = ApiClient.service.listConversations(cursor = cursor)
                // Bails WITH clearing loadingMore, not before it -- it was
                // set true just above, and a newer refresh() (which bumped
                // loadGeneration) never touches this flag itself, so nothing
                // else would ever clear it for this launch once stale.
                if (generation != loadGeneration) {
                    _state.value = _state.value.copy(loadingMore = false)
                    return@launch
                }
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(
                        conversations = _state.value.conversations + resp.body().orEmpty(),
                        loadingMore = false,
                        nextCursor = resp.headers()["X-Next-Cursor"],
                    )
                } else {
                    _state.value = _state.value.copy(loadingMore = false, error = resp.describeError("Couldn't load more history"))
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                _state.value = _state.value.copy(
                    loadingMore = false,
                    error = if (generation == loadGeneration) "Can't reach the server. Check your connection." else _state.value.error,
                )
            }
        }
    }
}
