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
    val feedback_rating: String? = null,
    val is_saved: Boolean = false,
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

@Serializable
data class ApiErrorBody(val detail: String? = null)

@Serializable
data class SavedAnswerOut(
    val conversation_id: Int,
    val machine_label: String? = null,
    val question: String? = null,
    val answer: MessageOut,
)
