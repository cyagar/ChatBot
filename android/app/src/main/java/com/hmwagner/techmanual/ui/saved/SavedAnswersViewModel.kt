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
                    _state.value = _state.value.copy(answers = resp.body().orEmpty(), loading = false)
                } else {
                    _state.value = _state.value.copy(loading = false, error = "Couldn't load saved answers (code ${resp.code()}).")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(loading = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }
}
