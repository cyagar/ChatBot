package com.hmwagner.techmanual.ui.common

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Logout
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.rememberCoroutineScope
import com.hmwagner.techmanual.network.ApiClient
import kotlinx.coroutines.launch

/**
 * A visible logout action, meant to be dropped into every signed-in
 * screen's TopAppBar `actions`. Sets ApiClient.sessionExpired on completion,
 * which is exactly the flag AppNav's existing 401-redirect already reacts
 * to -- a deliberate logout needs the identical "go to login and drop every
 * account-scoped screen" handling a session expiry does, so it reuses that
 * one path instead of a second, easy-to-drift redirect implementation.
 */
@Composable
fun LogoutAction() {
    val scope = rememberCoroutineScope()
    IconButton(onClick = { scope.launch { ApiClient.logout() } }) {
        Icon(Icons.AutoMirrored.Filled.Logout, contentDescription = "Log out")
    }
}
