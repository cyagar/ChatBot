package com.hmwagner.techmanual.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Test

class NoAnswerHeadingTest {
    @Test
    fun `each reason gets a distinct, specific heading`() {
        assertEquals(
            "Manual search is temporarily unavailable",
            noAnswerHeading("service_unavailable"),
        )
        assertEquals(
            "No approved manual is on file yet for this machine",
            noAnswerHeading("no_eligible_manual"),
        )
        assertEquals(
            "I couldn't confirm this answer in the approved manuals",
            noAnswerHeading("insufficient_evidence"),
        )
        assertEquals(
            "I couldn't confirm this answer in the approved manuals",
            noAnswerHeading("model_abstained"),
        )
        assertEquals(
            "I couldn't confirm this answer in the approved manuals",
            noAnswerHeading("validation_rejected"),
        )
    }

    @Test
    fun `an unknown or missing reason falls back to the generic heading`() {
        assertEquals("No verified answer from the approved manuals", noAnswerHeading(null))
        assertEquals("No verified answer from the approved manuals", noAnswerHeading("some_future_reason_this_apk_predates"))
    }
}
