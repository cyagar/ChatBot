package com.hmwagner.techmanual.network

import kotlinx.serialization.json.Json
import retrofit2.Response

/**
 * P2-07 (external review, 2026-09-21): "correlation IDs are already in the
 * error envelope; show them in Android/admin errors" -- admin.js already
 * does this (P1-08's showError/api()); this is the Android side. Every
 * error response body already carries a correlation_id
 * (backend/app/api/errors.py's error_body), but nothing in Android ever
 * read it -- a technician's "couldn't save (code 500)" report gave support
 * nothing to search the backend's logs by.
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
 * Builds a user-facing error message: `"$action (code $code)"`, plus a
 * `, ref: <correlation_id>` suffix when the response body carried one. Call
 * this at most once per response, and only on a response already known to
 * be `!isSuccessful` -- like parsedErrorBody, it consumes errorBody()'s
 * stream.
 */
fun <T> Response<T>.describeError(action: String): String {
    val correlationId = parsedErrorBody()?.correlation_id
    return if (correlationId != null) {
        "$action (code ${code()}, ref: $correlationId)."
    } else {
        "$action (code ${code()})."
    }
}
