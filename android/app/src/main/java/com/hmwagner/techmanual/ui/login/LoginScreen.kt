package com.hmwagner.techmanual.ui.login

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.foundation.layout.size
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LoginScreen(onLoggedIn: () -> Unit, vm: LoginViewModel = viewModel()) {
    val state by vm.state.collectAsState()

    LaunchedEffect(state.loggedIn) {
        if (state.loggedIn) onLoggedIn()
    }

    Scaffold { padding ->
        Box(Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
            Column(
                modifier = Modifier.widthIn(max = 420.dp).padding(24.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp),
            ) {
                Text("Technician Manual Assistant", style = androidx.compose.material3.MaterialTheme.typography.headlineSmall)
                Text("Sign in to look up machine manuals and get grounded answers.")

                OutlinedTextField(
                    value = state.email,
                    onValueChange = vm::onEmailChange,
                    label = { Text("Email") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email),
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    value = state.password,
                    onValueChange = vm::onPasswordChange,
                    label = { Text("Password") },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    modifier = Modifier.fillMaxWidth(),
                )

                if (state.error != null) {
                    Text(
                        state.error!!,
                        color = androidx.compose.material3.MaterialTheme.colorScheme.error,
                        modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
                    )
                }

                Button(
                    onClick = vm::login,
                    enabled = !state.loading,
                    modifier = Modifier.fillMaxWidth().semantics {
                        if (state.loading) contentDescription = "Signing in"
                    },
                ) {
                    if (state.loading) {
                        CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                    } else {
                        Text("Sign in")
                    }
                }
            }
        }
    }
}
