package com.hmwagner.techmanual.util

import java.time.ZoneId
import java.util.Locale
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class DisplayTimeTest {

    @Test
    fun `a UTC timestamp is shown in the requested time zone`() {
        val utc = formatLocalTimestamp("2026-09-24T19:40:12Z", ZoneId.of("UTC"), Locale.US)
        val newYork = formatLocalTimestamp("2026-09-24T19:40:12Z", ZoneId.of("America/New_York"), Locale.US)
        assertTrue(utc, utc.contains("7:40"))
        assertTrue(newYork, newYork.contains("3:40"))
    }

    @Test
    fun `fractional seconds and explicit offsets parse`() {
        val out = formatLocalTimestamp("2026-09-24T19:40:12.123456+00:00", ZoneId.of("UTC"), Locale.US)
        assertTrue(out, out.contains("7:40"))
    }

    @Test
    fun `an unparseable value falls back to its readable prefix instead of throwing`() {
        assertEquals("not a timestamp!", formatLocalTimestamp("not a timestamp!! extra", ZoneId.of("UTC"), Locale.US))
        assertEquals("", formatLocalTimestamp("", ZoneId.of("UTC"), Locale.US))
    }
}
