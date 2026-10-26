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
    fun `describeError uses the server's own detail text, with the correlation id appended as a ref`() = runBlocking {
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

        // The server's own specific detail wins over the generic
        // "$action (code N)" wrapper -- that generic form is only a
        // fallback for when the body carries no detail at all (below).
        assertEquals("Something broke (ref: abc-123-def)", message)
    }

    @Test
    fun `describeError falls back to the message field when detail is blank`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(500)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"detail": "", "message": "Something broke", "correlation_id": "abc-123-def"}"""),
        )

        val resp = service.me()
        val message = resp.describeError("Couldn't load this")

        assertEquals("Something broke (ref: abc-123-def)", message)
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

    @Test
    fun `describeError uses the server detail with no ref suffix when there is no correlation id`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(409)
                .setHeader("Content-Type", "application/json")
                .setBody("""{"detail": "An account with this email already exists."}"""),
        )

        val resp = service.me()
        val message = resp.describeError("Couldn't register")

        assertEquals("An account with this email already exists.", message)
    }

    @Test
    fun `describeErrorWithCode surfaces retryable and field_errors from the body`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(422)
                .setHeader("Content-Type", "application/json")
                .setBody(
                    """{"detail": "Invalid request.", "code": "VALIDATION_ERROR", "retryable": false,
                        "field_errors": [{"field": "email", "message": "not a valid email"}]}""",
                ),
        )

        val resp = service.me()
        val failure = resp.describeErrorWithCode("Couldn't submit")

        assertEquals("VALIDATION_ERROR", failure.code)
        assertEquals(false, failure.retryable)
        assertEquals(1, failure.fieldErrors.size)
        assertEquals("email", failure.fieldErrors[0].field)
        assertEquals("not a valid email", failure.fieldErrors[0].message)
    }
}
