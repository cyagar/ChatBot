package com.hmwagner.techmanual.util

/**
 * Best-effort dotted-numeric-version comparison for BuildConfig.VERSION_NAME
 * against the backend's minimum_supported_version. Neither string is guaranteed proper semver, so this only
 * compares the leading run of dot-separated integers and ignores any trailing suffix. Fails open
 * (returns false, "not below minimum") on anything it can't confidently
 * parse as at least one integer: an ambiguous or malformed version string is
 * not grounds to lock a technician out of the app.
 */
fun isVersionBelowMinimum(current: String, minimum: String): Boolean {
    val currentParts = leadingNumericParts(current)
    val minimumParts = leadingNumericParts(minimum)
    if (currentParts.isEmpty() || minimumParts.isEmpty()) return false

    for (i in 0 until maxOf(currentParts.size, minimumParts.size)) {
        val c = currentParts.getOrElse(i) { 0 }
        val m = minimumParts.getOrElse(i) { 0 }
        if (c != m) return c < m
    }
    return false
}

private fun leadingNumericParts(version: String): List<Int> =
    version.trim()
        .takeWhile { it.isDigit() || it == '.' }
        .split(".")
        .mapNotNull { it.toIntOrNull() }
