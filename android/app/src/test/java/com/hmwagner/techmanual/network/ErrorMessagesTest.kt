package com.hmwagner.techmanual.network

import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test
import retrofit2.Retrofit

/**
 * describeError() reads the backend's correlation_id and surfaces it in a
 * user-facing error message. Uses a real MockWebServer + Retrofit round trip (not a
 * hand-built Response) since Response.errorBody() behavior is tied to a
 * real OkHttp response.
 */
class ErrorMessagesTest {

    private lateinit var server: MockWebServer
    private lateinit var service: ApiService

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
        val retrofit = Retrofit.Builder()
            .baseUrl(server.url("/"))
            .client(OkHttpClient.Builder().retryOnConnectionFailure(false).build())
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
        service = retrofit.create(ApiService::class.java)
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun `describeError includes the correlation id when the error body carries one`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(500)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"detail": "Something broke", "code": "INTERNAL", "message": "Something broke",
                        "correlation_id": "abc-123-def", "retryable": false, "field_errors": [], "status": 500}""",
                ),
        )

        val resp = service.me()
        val message = resp.describeError("Couldn't load this")

        assertEquals("Couldn't load this (code 500, ref: abc-123-def).", message)
    }

    @Test
    fun `describeError omits the ref suffix when the error body has no correlation id`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(503))

        val resp = service.me()
        val message = resp.describeError("Couldn't load this")

        assertEquals("Couldn't load this (code 503).", message)
    }

    @Test
    fun `describeError omits the ref suffix when the error body is not valid JSON`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(502)
                .setHeader("Content-Type", "text/html")
                .setBody("<html>Bad Gateway</html>"),
        )

        val resp = service.me()
        val message = resp.describeError("Couldn't load this")

        assertEquals("Couldn't load this (code 502).", message)
    }
}
