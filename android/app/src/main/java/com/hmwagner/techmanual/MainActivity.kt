package com.hmwagner.techmanual

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.material3.windowsizeclass.ExperimentalMaterial3WindowSizeClassApi
import androidx.compose.material3.windowsizeclass.calculateWindowSizeClass
import androidx.compose.ui.Modifier
import com.hmwagner.techmanual.ui.nav.AppNav
import com.hmwagner.techmanual.ui.theme.TechManualTheme

class MainActivity : ComponentActivity() {
    @OptIn(ExperimentalMaterial3WindowSizeClassApi::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            TechManualTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    val windowSizeClass = calculateWindowSizeClass(this)
                    AppNav(windowSizeClass = windowSizeClass)
                }
            }
        }
    }
}
