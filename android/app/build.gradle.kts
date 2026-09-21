import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

// P0A-6: BASE_URL used to be a hardcoded personal LAN IP checked into git,
// with no way to change it without editing this file and rebuilding. Now
// read from local.properties (already gitignored -- see local.properties.example
// for the keys) or an environment variable of the same name, so a per-developer
// or CI endpoint never needs a source edit or a commit.
val localProperties = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

fun resolveBaseUrl(propertyKey: String, envVar: String, default: String): String =
    (System.getenv(envVar) ?: localProperties.getProperty(propertyKey) ?: default).trim()

// No real device/emulator reaches a personal LAN IP by default, so debug
// falls back to the emulator's host-loopback alias -- works out of the box
// for anyone using an emulator; a physical device needs one local.properties
// line (see README "Configuring the backend endpoint").
val debugBaseUrl = resolveBaseUrl(
    "techManual.baseUrl.debug", "TECHMANUAL_BASE_URL_DEBUG", "http://10.0.2.2:8000/"
)
val releaseBaseUrlPlaceholder = "https://CHANGE-ME.invalid/"
val releaseBaseUrl = resolveBaseUrl(
    "techManual.baseUrl.release", "TECHMANUAL_BASE_URL_RELEASE", releaseBaseUrlPlaceholder
)

// Release signing key. Same local.properties-or-env-var pattern as BASE_URL
// above -- never committed (android/.gitignore blocks /keystore/, *.jks,
// *.keystore), so a release build fails loudly (verifyReleaseSigningConfig
// below) rather than silently falling back to an ad-hoc debug-style key.
// Losing this key permanently is worse than never having built a release at
// all: every future update needs the SAME key to install over an existing
// one, so back up android/keystore/release.jks and its passwords somewhere
// durable outside this machine, not just here.
fun resolveSigningProperty(propertyKey: String, envVar: String): String? =
    (System.getenv(envVar) ?: localProperties.getProperty(propertyKey))?.trim()?.ifEmpty { null }

val releaseStoreFilePath = resolveSigningProperty("techManual.release.storeFile", "TECHMANUAL_RELEASE_STORE_FILE")
val releaseStorePassword = resolveSigningProperty("techManual.release.storePassword", "TECHMANUAL_RELEASE_STORE_PASSWORD")
val releaseKeyAlias = resolveSigningProperty("techManual.release.keyAlias", "TECHMANUAL_RELEASE_KEY_ALIAS")
val releaseKeyPassword = resolveSigningProperty("techManual.release.keyPassword", "TECHMANUAL_RELEASE_KEY_PASSWORD")

android {
    namespace = "com.hmwagner.techmanual"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.hmwagner.techmanual"
        // Placeholder pending the Phase 0 fleet inventory (plan section 6) --
        // this is not a real minSdk decision, just a safe floor for the demo.
        minSdk = 26
        targetSdk = 37
        versionCode = 1
        versionName = "0.1.0-demo"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        // Only registered when every value is actually present, so a
        // debug-only checkout (no keystore, nothing in local.properties)
        // never fails Gradle sync -- verifyReleaseSigningConfig below is
        // what turns "unconfigured" into a build failure, and only for
        // assembleRelease/bundleRelease specifically.
        if (releaseStoreFilePath != null && releaseStorePassword != null &&
            releaseKeyAlias != null && releaseKeyPassword != null) {
            create("release") {
                storeFile = rootProject.file(releaseStoreFilePath)
                storePassword = releaseStorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
            // Dev backend reachable over plain HTTP; src/debug/res/xml/
            // network_security_config_debug.xml permits cleartext broadly
            // for this build type only, so any dev-machine LAN IP or
            // hostname works here without also editing that file.
            buildConfigField("String", "BASE_URL", "\"$debugBaseUrl\"")
        }
        release {
            isMinifyEnabled = false
            buildConfigField("String", "BASE_URL", "\"$releaseBaseUrl\"")
            signingConfigs.findByName("release")?.let { signingConfig = it }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    testOptions {
        // Lets android.util.Log (used for best-effort-failure visibility,
        // e.g. MachinesViewModel.touchMachineBestEffort) return its default
        // rather than throwing "not mocked" in plain JVM unit tests.
        unitTests.isReturnDefaultValues = true
    }
}

// Fails a release build with no real HTTPS endpoint configured, instead of
// silently shipping the https://CHANGE-ME.invalid/ placeholder. Registered
// as its own task and wired in via dependsOn (not a check inline in the
// release {} block above) so it only runs -- and only fails -- when a
// release-assembling task actually executes; assembleDebug,
// testDebugUnitTest, and connectedDebugAndroidTest never evaluate it.
tasks.register("verifyReleaseBaseUrl") {
    doLast {
        check(releaseBaseUrl.startsWith("https://") && releaseBaseUrl != releaseBaseUrlPlaceholder) {
            "Release BASE_URL is not configured (currently '$releaseBaseUrl'). Set " +
                "techManual.baseUrl.release in local.properties, or the " +
                "TECHMANUAL_BASE_URL_RELEASE environment variable, to a real https:// " +
                "endpoint before building a release variant."
        }
    }
}

tasks.register("verifyReleaseSigningConfig") {
    doLast {
        check(android.signingConfigs.findByName("release") != null) {
            "Release signing is not configured. Set techManual.release.storeFile/" +
                "storePassword/keyAlias/keyPassword in local.properties (or the matching " +
                "TECHMANUAL_RELEASE_* environment variables) before building a release " +
                "variant -- see local.properties.example."
        }
    }
}

tasks.matching { it.name == "assembleRelease" || it.name == "bundleRelease" }.configureEach {
    dependsOn("verifyReleaseBaseUrl", "verifyReleaseSigningConfig")
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2026.08.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material3:material3-window-size-class")
    implementation("androidx.compose.material:material-icons-extended")
    debugImplementation("androidx.compose.ui:ui-tooling")

    implementation("androidx.core:core-ktx:1.19.0")
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.11.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.11.0")
    implementation("androidx.navigation:navigation-compose:2.9.8")

    implementation("com.squareup.retrofit2:retrofit:2.12.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")
    implementation("com.jakewharton.retrofit:retrofit2-kotlinx-serialization-converter:1.0.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.9.0")

    implementation("io.coil-kt.coil3:coil-compose:3.5.0")
    implementation("io.coil-kt.coil3:coil-network-okhttp:3.5.0")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.10.1")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")

    // Instrumented (on-device) tests only -- Compose UI test + real
    // AndroidX Test runner, needed because the session-expiry redirect this
    // covers depends on real Android Keystore/SharedPreferences behavior in
    // PersistentCookieJar, which the JVM-only unit tests above can't
    // exercise. See android/app/src/androidTest.
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
    androidTestImplementation("androidx.test:runner:1.7.0")
    androidTestImplementation(platform("androidx.compose:compose-bom:2026.08.00"))
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
    androidTestImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.10.1")
    androidTestImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
}
