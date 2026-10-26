package com.hmwagner.techmanual.ui.saved

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.SavedAnswerOut
import com.hmwagner.techmanual.network.describeError
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

data class SavedAnswersUiState(
    val answers: List<SavedAnswerOut> = emptyList(),
    val loading: Boolean = true,
    val loadingMore: Boolean = false,
    val nextCursor: String? = null,
    val error: String? = null,
)

/**
 * The discover/browse surface for the backend's saved-answers capability
 * (POST /messages/{id}/save, used from ChatScreen's "Save" link) -- mirrors
 * HistoryViewModel's shape/no-init-refresh pattern (the screen's own
 * LaunchedEffect(Unit) covers first load).
 */
class SavedAnswersViewModel : ViewModel() {
    private val _state = MutableStateFlow(SavedAnswersUiState())
    val state: StateFlow<SavedAnswersUiState> = _state

    // Same reasoning as HistoryViewModel.loadGeneration: without this, a
    // pull-to-refresh racing a pagination load in flight could let the
    // slower response overwrite the other's result with a stale list.
    private var loadGeneration = 0

    fun refresh() {
        val generation = ++loadGeneration
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, error = null)
            try {
                val resp = ApiClient.service.listSavedAnswers()
                if (generation != loadGeneration) return@launch
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(
                        answers = resp.body().orEmpty(),
                        loading = false,
                        nextCursor = resp.headers()["X-Next-Cursor"],
                    )
                } else {
                    _state.value = _state.value.copy(loading = false, error = resp.describeError("Couldn't load saved answers"))
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                if (generation != loadGeneration) return@launch
                _state.value = _state.value.copy(loading = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }

    // Requests a page past the first, same as HistoryViewModel.loadMore.
    fun loadMore() {
        val cursor = _state.value.nextCursor ?: return
        if (_state.value.loadingMore) return
        val generation = loadGeneration
        viewModelScope.launch {
            _state.value = _state.value.copy(loadingMore = true, error = null)
            try {
                val resp = ApiClient.service.listSavedAnswers(cursor = cursor)
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
                        answers = _state.value.answers + resp.body().orEmpty(),
                        loadingMore = false,
                        nextCursor = resp.headers()["X-Next-Cursor"],
                    )
                } else {
                    _state.value = _state.value.copy(loadingMore = false, error = resp.describeError("Couldn't load more saved answers"))
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

    // Removes an entry from this list (see routes_chat.py's unsave_answer,
    // POST .../unsave). Removes the row optimistically so the tap feels
    // immediate. On failure, re-inserts only THIS item back into whatever
    // the list currently is -- restoring the whole pre-removal snapshot
    // instead would resurrect any OTHER item a second, concurrent unsave()
    // had already successfully removed in the meantime.
    fun unsave(messageId: Int) {
        val before = _state.value.answers
        val removedIndex = before.indexOfFirst { it.answer.id == messageId }
        if (removedIndex == -1) return
        val removedItem = before[removedIndex]
        _state.value = _state.value.copy(answers = before.filterNot { it.answer.id == messageId })
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.unsaveAnswer(messageId)
                if (!resp.isSuccessful) {
                    restoreAnswer(removedItem, removedIndex, resp.describeError("Couldn't remove that saved answer"))
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                restoreAnswer(removedItem, removedIndex, "Can't reach the server. Check your connection.")
            }
        }
    }

    private fun restoreAnswer(item: SavedAnswerOut, index: Int, error: String) {
        val current = _state.value.answers
        // Already back (e.g. a refresh() landed in the meantime and brought
        // it back on its own) -- re-inserting again would duplicate the row.
        if (current.any { it.answer.id == item.answer.id }) {
            _state.value = _state.value.copy(error = error)
            return
        }
        val restored = current.toMutableList().apply { add(index.coerceAtMost(size), item) }
        _state.value = _state.value.copy(answers = restored, error = error)
    }
}
