package com.hmwagner.techmanual.util

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VersionCompareTest {

    @Test
    fun `a strictly older current version is below the minimum`() {
        assertTrue(isVersionBelowMinimum("0.1.0", "0.2.0"))
        assertTrue(isVersionBelowMinimum("1.0.0", "1.0.1"))
        assertTrue(isVersionBelowMinimum("1.9.9", "2.0.0"))
    }

    @Test
    fun `an equal or newer current version is not below the minimum`() {
        assertFalse(isVersionBelowMinimum("0.2.0", "0.2.0"))
        assertFalse(isVersionBelowMinimum("0.3.0", "0.2.0"))
        assertFalse(isVersionBelowMinimum("2.0.0", "1.9.9"))
    }

    @Test
    fun `a demo suffix is ignored, comparing only the leading numeric parts`() {
        assertFalse(isVersionBelowMinimum("0.1.0-demo", "0.1.0"))
        assertTrue(isVersionBelowMinimum("0.1.0-demo", "0.2.0"))
    }

    @Test
    fun `an unparseable version fails open rather than blocking`() {
        assertFalse(isVersionBelowMinimum("", "0.2.0"))
        assertFalse(isVersionBelowMinimum("0.1.0", ""))
        assertFalse(isVersionBelowMinimum("not-a-version", "0.2.0"))
        assertFalse(isVersionBelowMinimum("0.1.0", "also-not-a-version"))
    }

    @Test
    fun `a shorter version string is padded with zeros, not treated as smaller by length`() {
        assertFalse(isVersionBelowMinimum("1.2", "1.2.0"))
        assertTrue(isVersionBelowMinimum("1.2", "1.2.1"))
    }
}
