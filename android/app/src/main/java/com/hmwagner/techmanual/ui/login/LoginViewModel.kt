package com.hmwagner.techmanual.ui.login

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.LoginRequest
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

data class LoginUiState(
    val email: String = "",
    val password: String = "",
    val loading: Boolean = false,
    val error: String? = null,
    val loggedIn: Boolean = false,
)

class LoginViewModel : ViewModel() {
    private val _state = MutableStateFlow(LoginUiState())
    val state: StateFlow<LoginUiState> = _state

    fun onEmailChange(v: String) { _state.value = _state.value.copy(email = v, error = null) }
    fun onPasswordChange(v: String) { _state.value = _state.value.copy(password = v, error = null) }

    fun login() {
        val s = _state.value
        if (s.email.isBlank() || s.password.isBlank()) {
            _state.value = s.copy(error = "Enter your email and password.")
            return
        }
        _state.value = s.copy(loading = true, error = null)
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.login(LoginRequest(s.email.trim(), s.password))
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(loading = false, loggedIn = true)
                } else {
                    _state.value = _state.value.copy(loading = false, error = errorMessage(resp.code()))
                }
            } catch (e: Exception) {
                _state.value = _state.value.copy(loading = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }

    private fun errorMessage(code: Int): String = when (code) {
        401 -> "Incorrect email or password."
        429 -> "Too many attempts. Wait a moment and try again."
        else -> "Sign-in failed (code $code)."
    }
}
