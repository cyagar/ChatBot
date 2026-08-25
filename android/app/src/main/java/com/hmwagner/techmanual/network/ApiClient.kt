package com.hmwagner.techmanual.network

import android.content.Context
import coil3.ImageLoader
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import com.hmwagner.techmanual.BuildConfig
import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit

/**
 * Hand-rolled singleton holder -- deliberately skipping Hilt today (see
 * android/README.md "Scope decisions for the one-day demo"). Retrofitting
 * proper DI is cheap once there's more than one screen's worth of wiring;
 * it wasn't worth the kapt/ksp setup time for a one-day vertical slice.
 */
object ApiClient {

    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    private lateinit var cookieJar: PersistentCookieJar
    lateinit var okHttpClient: OkHttpClient
        private set
    lateinit var service: ApiService
        private set
    lateinit var imageLoader: ImageLoader
        private set

    // Flips true the moment any authenticated call comes back 401 (session
    // expired, revoked, or the user was disabled mid-session -- get_current_user
    // in the backend rejects all of these the same way). AppNav observes this
    // and routes back to the login screen -- otherwise every screen just shows
    // a raw "code 401" error forever with no way back in (session TTL is short
    // enough that this WILL happen if the app sits idle during the demo).
    private val _sessionExpired = MutableStateFlow(false)
    val sessionExpired: StateFlow<Boolean> = _sessionExpired

    fun onSessionExpiredHandled() {
        _sessionExpired.value = false
    }

    fun init(context: Context) {
        if (::service.isInitialized) return

        cookieJar = PersistentCookieJar(context)

        val logging = HttpLoggingInterceptor().apply {
            level = if (BuildConfig.DEBUG) HttpLoggingInterceptor.Level.BASIC else HttpLoggingInterceptor.Level.NONE
        }

        val authExpiryInterceptor = okhttp3.Interceptor { chain ->
            val request = chain.request()
            val response = chain.proceed(request)
            // Login itself legitimately returns 401 for bad credentials --
            // that's not a session expiry and must not bounce the login
            // screen back to itself.
            if (response.code == 401 && !request.url.encodedPath.endsWith("/api/auth/login")) {
                cookieJar.clear()
                _sessionExpired.value = true
            }
            response
        }

        okHttpClient = OkHttpClient.Builder()
            .cookieJar(cookieJar)
            .addInterceptor(logging)
            .addInterceptor(authExpiryInterceptor)
            // Retrieval + provider call runs synchronously server-side (see
            // routes_chat.py's ask_question) -- a real answer can take 20-30s.
            .readTimeout(90, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .connectTimeout(15, TimeUnit.SECONDS)
            // askQuestion sends a per-turn Idempotency-Key (see ChatViewModel.send
            // / routes_chat.py's ask_question dedup on conversation_id+key), so a
            // retried POST is now safe to resend rather than something to guard
            // against -- retry-on-connection-failure left enabled (the default)
            // covers the "unexpected end of stream" class of error (a dead pooled
            // connection reused after the peer's keep-alive timeout elapsed;
            // confirmed 2026-08-25 the local dev server's default 5s
            // --timeout-keep-alive was the actual trigger during testing, but a
            // real network can still drop an idle connection this way).
            .build()

        val retrofit = Retrofit.Builder()
            .baseUrl(BuildConfig.BASE_URL)
            .client(okHttpClient)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()

        service = retrofit.create(ApiService::class.java)

        imageLoader = ImageLoader.Builder(context)
            .components { add(OkHttpNetworkFetcherFactory(callFactory = { okHttpClient })) }
            .build()
    }

    fun hasSession(): Boolean = cookieJar.hasSession()

    fun clearSession() = cookieJar.clear()

    // Test seam only. init(context) needs a real Android Context, which
    // JVM-only ViewModel unit tests don't have -- tests build their own
    // Retrofit/OkHttp pointed at a MockWebServer and swap it in here instead.
    fun overrideServiceForTest(testService: ApiService) {
        service = testService
    }
}
