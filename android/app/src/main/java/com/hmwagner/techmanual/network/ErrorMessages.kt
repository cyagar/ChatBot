package com.hmwagner.techmanual.network

import kotlinx.serialization.json.Json
import retrofit2.Response

/**
 * Every error response body carries a correlation_id
 * (backend/app/api/errors.py's error_body); reading it here lets a
 * technician's "couldn't save (code 500)" report give support something to
 * search the backend's logs by, matching admin.js's showError/api() on the
 * web side.
 */
private val errorJson = Json { ignoreUnknownKeys = true }

/**
 * A failed Retrofit response's error body, parsed once. Safe to call
 * exactly once per response -- Response.errorBody() consumes the
 * underlying stream, same as any other OkHttp response body.
 */
private fun <T> Response<T>.parsedErrorBody(): ApiErrorBody? {
    val raw = errorBody()?.string() ?: return null
    return try {
        errorJson.decodeFromString<ApiErrorBody>(raw)
    } catch (_: Exception) {
        null
    }
}

/**
 * Builds a user-facing error message: the server's own `detail` text when
 * the response body carried one (e.g. "This invitation has already been
 * used."), falling back to the generic `"$action (code $code)"` only when
 * it didn't -- plus a `(ref: <correlation_id>)` suffix either way, when the
 * body carried one. Call this at most once per response, and only on a
 * response already known to be `!isSuccessful` -- like parsedErrorBody, it
 * consumes errorBody()'s stream.
 */
fun <T> Response<T>.describeError(action: String): String = describeErrorWithCode(action).message

/**
 * A failed response's domain error `code` (e.g. CONVERSATION_BUSY) alongside
 * the user-facing message, whether the server itself considers this
 * retryable, and any per-field validation messages (a 422's field_errors) --
 * all three available to a caller that wants to do more than just display
 * `message` (e.g. retry automatically, or highlight a specific form field).
 */
data class DescribedError(
    val code: String?,
    val message: String,
    val retryable: Boolean = false,
    val fieldErrors: List<ApiFieldError> = emptyList(),
)

fun <T> Response<T>.describeErrorWithCode(action: String): DescribedError {
    val body = parsedErrorBody()
    val correlationId = body?.correlation_id
    val detail = body?.detail?.takeIf { it.isNotBlank() } ?: body?.message?.takeIf { it.isNotBlank() }
    val message = when {
        detail != null && correlationId != null -> "$detail (ref: $correlationId)"
        detail != null -> detail
        correlationId != null -> "$action (code ${code()}, ref: $correlationId)."
        else -> "$action (code ${code()})."
    }
    return DescribedError(
        code = body?.code,
        message = message,
        retryable = body?.retryable ?: false,
        fieldErrors = body?.field_errors.orEmpty(),
    )
}
