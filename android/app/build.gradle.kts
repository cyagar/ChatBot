plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

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
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
            // Dev backend reachable over plain HTTP on the local network; see
            // src/debug/res/xml/network_security_config_debug.xml for the
            // matching cleartext exception, which only applies to this build type.
            buildConfigField("String", "BASE_URL", "\"http://192.168.1.71:8000/\"")
        }
        release {
            isMinifyEnabled = false
            buildConfigField("String", "BASE_URL", "\"https://CHANGE-ME.invalid/\"")
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
}
