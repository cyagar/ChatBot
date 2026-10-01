package com.hmwagner.techmanual.util

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VersionCompareTest {

    @Test
    fun `a strictly lower version code is below the minimum`() {
        assertTrue(isVersionCodeBelowMinimum(5, 6))
        assertTrue(isVersionCodeBelowMinimum(1, 2))
    }

    @Test
    fun `an equal or higher version code is not below the minimum`() {
        assertFalse(isVersionCodeBelowMinimum(6, 6))
        assertFalse(isVersionCodeBelowMinimum(7, 6))
    }

    @Test
    fun `an unset minimum (0 or negative) never blocks`() {
        assertFalse(isVersionCodeBelowMinimum(1, 0))
        assertFalse(isVersionCodeBelowMinimum(1, -1))
    }

    @Test
    fun `builds that share one versionName are still told apart by versionCode`() {
        // This app's own versionName stayed "1.0.0" across versionCode 2
        // through 6 -- the exact scenario a versionName-string comparison
        // could never resolve, which is why this compares versionCode.
        assertTrue(isVersionCodeBelowMinimum(2, 6))
        assertFalse(isVersionCodeBelowMinimum(6, 2))
    }
}
