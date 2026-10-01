package com.hmwagner.techmanual.util

/**
 * Whether `current` (BuildConfig.VERSION_CODE) is below `minimum` (a
 * server-advertised minimum_supported_version_code/latest_version_code).
 * Comparing the integer versionCode, not the dotted versionName string, is
 * deliberate: Gradle assigns versionCode a fresh, strictly increasing value
 * every build, while versionName is free text a developer sets by hand and
 * can easily forget to bump -- this app's own versionName stayed "1.0.0"
 * across versionCode 2 through 6, which would have made a versionName
 * comparison unable to tell any of those five builds apart.
 *
 * `minimum <= 0` means "unset" and never blocks -- 0 is the field's default
 * both in the backend settings and in a test fixture that doesn't mention
 * it, so an unconfigured deployment (or a test with no opinion on this)
 * must not accidentally lock a technician out of the app.
 */
fun isVersionCodeBelowMinimum(current: Int, minimum: Int): Boolean = minimum > 0 && current < minimum
