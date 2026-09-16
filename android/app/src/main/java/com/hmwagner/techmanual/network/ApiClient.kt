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
    // in the backend rejects all of these the same way), OR a deliberate
    // logout() below. AppNav observes this and routes back to the login
    // screen, clearing every account-scoped screen along the way -- otherwise
    // a signed-in screen either shows a raw "code 401" error forever with no
    // way back in, or (logout) just keeps showing the previous account's data
    // with no server session behind it (P0A-1).
    private val _sessionExpired = MutableStateFlow(false)
    val sessionExpired: StateFlow<Boolean> = _sessionExpired

    fun onSessionExpiredHandled() {
        _sessionExpired.value = false
    }

    /**
     * P0A-1: a visible, explicit sign-out, reachable from every signed-in
     * screen. Always ends the local session even if the server can't be
     * reached -- a technician must never be stuck unable to sign out (or
     * into a different account) just because the network is down -- but
     * still tries the real server-side logout first so the session is
     * actually revoked when possible, not just forgotten locally.
     */
    suspend fun logout() {
        try {
            service.logout()
        } catch (_: Exception) {
            // Best-effort; the local session is cleared unconditionally below.
        }
        cookieJar.clear()
        _sessionExpired.value = true
    }

    fun init(context: Context) {
        if (::service.isInitialized) return
        build(context, BuildConfig.BASE_URL)
    }

    // Instrumented-test seam only: init(context) is a one-shot guarded by
    // ::service.isInitialized, and by the time a test runs, TechManualApp's
    // real Application.onCreate has already called it against the real
    // BuildConfig.BASE_URL -- there's no way back in through init() alone.
    // This rebuilds the whole pipeline (fresh cookie jar included) against a
    // test-supplied base URL, e.g. a MockWebServer instance, so instrumented
    // tests exercise the real authExpiryInterceptor rather than bypassing it
    // the way overrideServiceForTest's JVM unit tests do.
    fun initForTest(context: Context, baseUrl: String) {
        _sessionExpired.value = false
        build(context, baseUrl)
    }

    private fun build(context: Context, baseUrl: String) {
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

        // Retries a request exactly once when the connection itself failed
        // (e.g. "unexpected end of stream" from a dead pooled connection
        // reused after the peer's keep-alive timeout elapsed -- confirmed
        // 2026-08-25 that a short server-side --timeout-keep-alive reliably
        // triggers this, and reconfirmed live 2026-09-16 hitting it three
        // times in ~10 minutes of normal tap-to-tap pacing during device
        // testing). GET is always safe to retry. Most POSTs are deliberately
        // NOT retried here, even though OkHttp's own retryOnConnectionFailure
        // would cover it too -- submitFeedback has no idempotency protection
        // (the feedback table is deliberately append-only; see
        // routes_chat.py's submit_feedback), so an automatic retry could write
        // a second row for a request the server actually received. ask_question
        // already has its own resilience story instead: a per-turn
        // Idempotency-Key plus the manual "Retry" button in ChatViewModel that
        // reuses it (see "Handled during review" in the README) -- that one
        // deliberately stays a user-initiated action, not an automatic one.
        //
        // POST /api/conversations is the one deliberate exception: picking a
        // machine hit this exact failure repeatedly during testing, and
        // unlike feedback it's genuinely safe to retry -- create_conversation
        // (routes_chat.py) is a single plain INSERT with no other side
        // effects, so the worst case of a retried request that the server
        // actually received is one harmless extra empty conversation, not a
        // duplicated write to an append-only table.
        val getRetryInterceptor = okhttp3.Interceptor { chain ->
            val request = chain.request()
            val retryable = request.method == "GET" ||
                (request.method == "POST" && request.url.encodedPath == "/api/conversations")
            if (!retryable) {
                chain.proceed(request)
            } else {
                try {
                    chain.proceed(request)
                } catch (_: java.io.IOException) {
                    chain.proceed(request)
                }
            }
        }

        okHttpClient = OkHttpClient.Builder()
            .cookieJar(cookieJar)
            .addInterceptor(logging)
            .addInterceptor(authExpiryInterceptor)
            .addInterceptor(getRetryInterceptor)
            // Retrieval + provider call runs synchronously server-side (see
            // routes_chat.py's ask_question) -- a real answer can take 20-30s.
            .readTimeout(90, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .connectTimeout(15, TimeUnit.SECONDS)
            // OkHttp's own blanket auto-retry is left off -- it can't
            // distinguish GET from POST, so it would retry submitFeedback too.
            // getRetryInterceptor above covers GET specifically instead.
            .retryOnConnectionFailure(false)
            .build()

        val retrofit = Retrofit.Builder()
            .baseUrl(baseUrl)
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
