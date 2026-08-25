package com.hmwagner.techmanual.network

import okhttp3.Cookie
import okhttp3.HttpUrl

/**
 * Pure domain+Set-Cookie-header serialization for the one cookie
 * PersistentCookieJar cares about, split out from it specifically so it's
 * testable from a plain JVM unit test -- the Keystore-backed encryption in
 * PersistentCookieJar itself is not (see ChatBot android/README.md's Tests
 * section for what device-only verification still covers).
 *
 * Cookie.toString() doesn't round-trip via Cookie.parse() on its own (parse
 * needs a URL to resolve the domain against), so this stores "domain||header"
 * pairs rather than trying to reconstruct a Cookie via its Builder from the
 * serialized form.
 */
internal object CookieSerialization {
    fun serialize(cookie: Cookie): String = "${cookie.domain}||${cookie}"

    fun parse(raw: String): Cookie? {
        val parts = raw.split("||", limit = 2)
        if (parts.size != 2) return null
        val (domain, header) = parts
        if (domain.isBlank()) return null
        val url = HttpUrl.Builder().scheme("http").host(domain).build()
        return Cookie.parse(url, header)
    }
}
