package com.hmwagner.techmanual.network

import kotlinx.serialization.Serializable

// Field names deliberately mirror the backend's JSON keys (snake_case) so no
// per-field @SerialName mapping is needed -- see backend/app/api/routes_*.py
// for the source of truth these mirror.

@Serializable
data class UserOut(
    val id: Int,
    val email: String,
    val role: String,
    val display_name: String? = null,
)

@Serializable
data class LoginRequest(val email: String, val password: String)

@Serializable
data class MachineOut(
    val id: Int,
    val manufacturer: String,
    val model_name: String,
    val family: String? = null,
    val machine_type: String? = null,
    val document_count: Int,
    val is_favorite: Boolean = false,
    val last_used_at: String? = null,
)

@Serializable
data class ConversationOut(
    val id: Int,
    val machine_id: Int? = null,
    val machine_label: String? = null,
    val title: String? = null,
    val started_at: String,
    val updated_at: String,
)

@Serializable
data class CreateConversationRequest(val machine_id: Int? = null)

@Serializable
data class SetMachineRequest(val machine_id: Int)

@Serializable
data class MessageIn(val content: String)

@Serializable
data class CitationOut(
    val chunk_id: Int,
    val document_id: Int,
    val filename: String,
    val title: String? = null,
    val page_number: Int? = null,
    val section_heading: String? = null,
    val revision: String? = null,
    val excerpt: String,
    // P0-13 (external review, 2026-09-21): the source document's CURRENT
    // review/withdrawal status, computed fresh by the backend on every
    // hydration -- an emergency withdrawal retroactively flags every
    // historical citation that pointed at it.
    val source_withdrawn: Boolean = false,
)

@Serializable
data class ClarifyingOption(val id: Int, val label: String)

@Serializable
data class MessageOut(
    val id: Int,
    val role: String,
    val content: String,
    val is_clarifying_question: Boolean = false,
    val is_no_answer: Boolean = false,
    val answer_status: String = "completed",
    val citations: List<CitationOut> = emptyList(),
    val safety_warnings: List<String> = emptyList(),
    val conflict_note: String? = null,
    val clarifying_options: List<ClarifyingOption> = emptyList(),
    val retry_count: Int = 0,
    val created_at: String,
    val is_saved: Boolean = false,
    // P0-13: true when any citation's source has been withdrawn or lost
    // approval since this answer was generated -- the UI must suppress
    // action-oriented styling and show a clear warning instead of presenting
    // this as current, trustworthy advice.
    val has_withdrawn_source: Boolean = false,
    // P1-02 (external review, 2026-09-21): the requesting user's own most
    // recent rating for this answer ("helpful" | "incorrect" |
    // "missing_info"), or null if never rated -- lets a reload show "already
    // rated" instead of resetting to blank buttons, the same reason
    // is_saved exists.
    val feedback_rating: String? = null,
)

@Serializable
data class FeedbackRequest(val rating: String, val comment: String? = null)

@Serializable
data class EvidenceOut(
    val chunk_id: Int,
    val content: String,
    val chunk_type: String? = null,
    val page_number: Int? = null,
    val section_heading: String? = null,
    val filename: String,
    val title: String? = null,
    val revision: String? = null,
    val doc_type: String? = null,
    val is_current_revision: Boolean = true,
    val has_page_image: Boolean = false,
)

// P2-07 (external review, 2026-09-21): defined since Phase 1 but never
// actually parsed anywhere -- every error message shown to a technician was
// a generic "(code 500)" with no reference the backend's own logs could be
// searched by, even though every error response already carries one (see
// backend/app/api/errors.py's error_body). ErrorMessages.kt's
// correlationSuffix() is what actually reads this now.
@Serializable
data class ApiErrorBody(val detail: String? = null, val correlation_id: String? = null)

@Serializable
data class SavedAnswerOut(
    val conversation_id: Int,
    val machine_label: String? = null,
    val question: String? = null,
    val answer: MessageOut,
)

// P1-23 (external review, 2026-09-21): GET /api/config existed since Phase 1
// but nothing in Android ever called it -- maintenance mode and a minimum
// supported version had no way to reach a technician.
@Serializable
data class ConfigOut(
    val maintenance_mode: Boolean,
    val maintenance_message: String,
    val minimum_supported_version: String,
    val support_contact: String,
    val status: String,
    val status_message: String,
)
