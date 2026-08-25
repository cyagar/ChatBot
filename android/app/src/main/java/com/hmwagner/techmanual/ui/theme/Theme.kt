package com.hmwagner.techmanual.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.ui.graphics.Color

private val Blue = Color(0xFF1857A6)
private val BlueDark = Color(0xFF9FC5FF)

private val LightColors = lightColorScheme(primary = Blue, secondary = Blue)
private val DarkColors = darkColorScheme(primary = BlueDark, secondary = BlueDark)

@Composable
fun TechManualTheme(content: @Composable () -> Unit) {
    val colors = if (isSystemInDarkTheme()) DarkColors else LightColors
    MaterialTheme(colorScheme = colors, content = content)
}

/**
 * Material3's ColorScheme has no built-in "warning" role (only error), but
 * safety warnings and revision-conflict notices need a color distinct from
 * both error and ordinary text. This was previously a single hardcoded hex
 * value used everywhere -- correct against the light scheme's surfaces, but
 * never re-checked against `DarkColors`, so it would render as a low
 * -contrast amber-on-dark-surface once a device actually renders in dark
 * mode. `0xFFFFB74D` here was picked against `DarkColors`' `surfaceVariant`/
 * `surfaceContainerHighest` (where warnings/conflict notes actually render,
 * inside message bubbles -- not the page background), not just eyeballed
 * against the window.
 */
val warningColor: Color
    @Composable
    @ReadOnlyComposable
    get() = if (isSystemInDarkTheme()) Color(0xFFFFB74D) else Color(0xFF8A5300)
