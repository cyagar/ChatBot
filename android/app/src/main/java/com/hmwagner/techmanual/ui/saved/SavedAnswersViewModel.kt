package com.hmwagner.techmanual.ui.saved

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.SavedAnswerOut
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
 * P2-33 (independent follow-up review): the backend's saved-answers
 * capability (POST /messages/{id}/save, used from ChatScreen's "Save" link)
 * had no discover/browse surface in Android -- a technician could mark an
 * answer saved but never see the list again. This is that list, mirroring
 * HistoryViewModel's shape/no-init-refresh pattern (the screen's own
 * LaunchedEffect(Unit) covers first load).
 */
class SavedAnswersViewModel : ViewModel() {
    private val _state = MutableStateFlow(SavedAnswersUiState())
    val state: StateFlow<SavedAnswersUiState> = _state

    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, error = null)
            try {
                val resp = ApiClient.service.listSavedAnswers()
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(
                        answers = resp.body().orEmpty(),
                        loading = false,
                        nextCursor = resp.headers()["X-Next-Cursor"],
                    )
                } else {
                    _state.value = _state.value.copy(loading = false, error = "Couldn't load saved answers (code ${resp.code()}).")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(loading = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }

    // P1-11 (external review, 2026-09-21): same gap as HistoryViewModel.loadMore
    // -- the backend has paginated GET /saved-answers since Phase 1, but
    // nothing in Android ever requested a page past the first.
    fun loadMore() {
        val cursor = _state.value.nextCursor ?: return
        if (_state.value.loadingMore) return
        viewModelScope.launch {
            _state.value = _state.value.copy(loadingMore = true, error = null)
            try {
                val resp = ApiClient.service.listSavedAnswers(cursor = cursor)
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(
                        answers = _state.value.answers + resp.body().orEmpty(),
                        loadingMore = false,
                        nextCursor = resp.headers()["X-Next-Cursor"],
                    )
                } else {
                    _state.value = _state.value.copy(loadingMore = false, error = "Couldn't load more saved answers (code ${resp.code()}).")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(loadingMore = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }

    // P1-21 (external review, 2026-09-21): this list had no way to remove an
    // entry -- see routes_chat.py's unsave_answer (POST .../unsave, added
    // alongside this). Removes the row optimistically so the tap feels
    // immediate; a failure restores it via a plain refresh() rather than
    // trying to re-insert the row in the right spot by hand.
    fun unsave(messageId: Int) {
        val before = _state.value.answers
        _state.value = _state.value.copy(answers = before.filterNot { it.answer.id == messageId })
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.unsaveAnswer(messageId)
                if (!resp.isSuccessful) {
                    _state.value = _state.value.copy(answers = before, error = "Couldn't remove that saved answer (code ${resp.code()}).")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(answers = before, error = "Can't reach the server. Check your connection.")
            }
        }
    }
}
