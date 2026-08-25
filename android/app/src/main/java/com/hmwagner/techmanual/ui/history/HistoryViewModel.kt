package com.hmwagner.techmanual.ui.history

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.ConversationOut
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

data class HistoryUiState(
    val conversations: List<ConversationOut> = emptyList(),
    val loading: Boolean = true,
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
                    _state.value = _state.value.copy(conversations = resp.body().orEmpty(), loading = false)
                } else {
                    _state.value = _state.value.copy(loading = false, error = "Couldn't load history (code ${resp.code()}).")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(loading = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }
}
