package com.hmwagner.techmanual.network

import okhttp3.Cookie
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Covers the pure, testable half of PersistentCookieJar's persistence -- the
 * Keystore-backed encryption around it isn't reachable from a JVM unit test
 * (see android/README.md's Tests section) and is only verified by a device
 * install.
 */
class CookieSerializationTest {

    private fun sessionCookie(domain: String = "192.168.1.71"): Cookie {
        val url = "http://$domain/".toHttpUrl()
        return Cookie.parse(url, "tma_session=abc123; Path=/; HttpOnly")!!
    }

    @Test
    fun `a serialized cookie round-trips back to an equivalent cookie`() {
        val original = sessionCookie()
        val restored = CookieSerialization.parse(CookieSerialization.serialize(original))

        assertEquals(original.name, restored?.name)
        assertEquals(original.value, restored?.value)
        assertEquals(original.domain, restored?.domain)
        assertEquals(original.httpOnly, restored?.httpOnly)
    }

    @Test
    fun `garbage input does not throw, it just fails to parse`() {
        assertNull(CookieSerialization.parse(""))
        assertNull(CookieSerialization.parse("no-separator-here"))
        assertNull(CookieSerialization.parse("||missing-domain-not-blank-check"))
    }

    @Test
    fun `a blank domain is rejected rather than building a bogus cookie`() {
        assertNull(CookieSerialization.parse("   ||tma_session=abc123"))
    }
}
