package com.hmwagner.techmanual.ui.chat

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.CitationOut
import com.hmwagner.techmanual.network.ConversationOut
import com.hmwagner.techmanual.network.EvidenceOut
import com.hmwagner.techmanual.network.MessageIn
import com.hmwagner.techmanual.network.MessageOut
import com.hmwagner.techmanual.network.SetMachineRequest
import java.util.UUID
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/**
 * A local-only echo of the technician's own typed question. The server never
 * returns the user's message from POST .../messages (only the assistant
 * reply) -- see routes_chat.py's ask_question -- so this is how the app shows
 * "what I just asked" before/while the answer arrives. It's overwritten by
 * the real persisted messages on the next full reload (retry, clarify
 * resume, screen re-entry), so it never drifts from the server's record.
 */
data class LocalEcho(val id: String, val content: String)

data class ChatUiState(
    val conversation: ConversationOut? = null,
    val messages: List<MessageOut> = emptyList(),
    val pendingEcho: LocalEcho? = null,
    // True only when a send failed with a network exception (not an HTTP
    // error) -- the one case where we genuinely don't know if the server got
    // the question (see the comment in send()'s catch block). Lets the UI
    // tell "still in flight" apart from "we lost track of this one".
    val pendingEchoUncertain: Boolean = false,
    val composerText: String = "",
    val sending: Boolean = false,
    val loadingHistory: Boolean = true,
    val error: String? = null,
    val evidence: EvidenceOut? = null,
    val evidenceDocumentId: Int? = null,
    val evidenceLoading: Boolean = false,
    // messageId -> rating, only for calls that actually succeeded. Lets
    // ChatScreen show "Marked helpful" instead of the buttons doing nothing
    // visible on tap (README: "no UI indication of success").
    val feedbackGiven: Map<Int, String> = emptyMap(),
    val savedMessageIds: Set<Int> = emptySet(),
)

class ChatViewModel(private val conversationId: Int) : ViewModel() {
    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state

    // Negative, decrementing, and never reused for the lifetime of this
    // ViewModel -- guaranteed not to collide with a real server-assigned
    // message id (always a positive DB primary key), which matters because
    // LazyColumn keys list items on MessageOut.id (see ChatScreen).
    private var nextLocalMessageId = -1

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch { loadMessages() }
    }

    // Split out from refresh() so selectClarifyingMachine() can await a reload
    // in the same coroutine instead of firing a detached one via refresh() --
    // launching a child coroutine and returning immediately let its own
    // `finally` clear `sending` before the reload actually finished, which
    // re-enabled the composer while the conversation was still mid-reload.
    private suspend fun loadMessages() {
        _state.value = _state.value.copy(loadingHistory = true, error = null)
        try {
            val msgs = ApiClient.service.getMessages(conversationId)
            if (msgs.isSuccessful) {
                val loaded = msgs.body().orEmpty()
                // Rehydrate from the server's own record of this user's
                // feedback/saves, not just the messages list itself -- a
                // freshly (re)created ChatViewModel (rotation between
                // panes, an app restart, or simply leaving and re-entering
                // this conversation) previously started both maps empty
                // every time, so the buttons reset to unmarked and a re-tap
                // silently duplicated the feedback/saved_answers row
                // server-side (found via live tablet testing 2026-08-25).
                _state.value = _state.value.copy(
                    messages = loaded,
                    loadingHistory = false,
                    pendingEcho = null,
                    pendingEchoUncertain = false,
                    feedbackGiven = loaded.mapNotNull { m -> m.feedback_rating?.let { m.id to it } }.toMap(),
                    savedMessageIds = loaded.filter { it.is_saved }.map { it.id }.toSet(),
                )
            } else {
                _state.value = _state.value.copy(loadingHistory = false, error = "Couldn't load this conversation (code ${msgs.code()}).")
            }
        } catch (_: Exception) {
            _state.value = _state.value.copy(loadingHistory = false, error = "Can't reach the server. Check your connection.")
        }
    }

    fun onComposerChange(v: String) {
        _state.value = _state.value.copy(composerText = v)
    }

    fun send() {
        val text = _state.value.composerText.trim()
        if (text.isEmpty() || _state.value.sending) return

        val echo = LocalEcho(UUID.randomUUID().toString(), text)
        _state.value = _state.value.copy(
            sending = true,
            error = null,
            composerText = "",
            pendingEcho = echo,
            pendingEchoUncertain = false,
        )
        viewModelScope.launch { performSend(echo) }
    }

    /**
     * Resends a still-pending question from an "uncertain" bubble (connection
     * lost mid-send, or the server says it's already processing) using the
     * SAME idempotency key and text as the original attempt -- never a
     * freshly generated key, and never whatever's currently typed in the
     * composer. Reusing the key is what makes this safe to tap more than
     * once: if the original attempt actually landed, the server replays its
     * result instead of creating a second user turn (routes_chat.py's
     * ask_question).
     */
    fun retryPendingSend() {
        val echo = _state.value.pendingEcho ?: return
        if (_state.value.sending) return
        _state.value = _state.value.copy(sending = true, error = null, pendingEchoUncertain = false)
        viewModelScope.launch { performSend(echo) }
    }

    private suspend fun performSend(echo: LocalEcho) {
        try {
            // echo.id doubles as the Idempotency-Key -- it's already a UUID
            // generated once per composed question and held steady across
            // retries (see LocalEcho/retryPendingSend), which is exactly what
            // the key needs to be.
            val resp = ApiClient.service.askQuestion(conversationId, MessageIn(echo.content), idempotencyKey = echo.id)
            when {
                resp.isSuccessful -> {
                    val answer = resp.body()!!
                    // The pendingEcho bubble was rendered AFTER the message
                    // list (see ChatScreen), so once the answer landed in
                    // `messages` while pendingEcho stayed set, the user's own
                    // question kept rendering below the assistant's reply --
                    // backwards conversation order. Fold the echoed question
                    // into `messages` as a synthetic local turn (the server
                    // never returns the user's own message from this
                    // endpoint -- see LocalEcho's doc comment) in the correct
                    // position, then clear pendingEcho so it isn't rendered twice.
                    val userTurn = MessageOut(
                        id = nextLocalMessageId--,
                        role = "user",
                        content = echo.content,
                        created_at = "",
                    )
                    _state.value = _state.value.copy(
                        sending = false,
                        messages = _state.value.messages + userTurn + answer,
                        pendingEcho = null,
                        pendingEchoUncertain = false,
                    )
                }
                resp.code() == 409 -> {
                    // The same idempotency key was already accepted -- the
                    // original attempt is still generating (or, rarely, the
                    // server died mid-attempt). Either way, starting a fresh
                    // attempt with a NEW key would be exactly the
                    // duplicate-question hazard idempotency keys exist to
                    // prevent, so keep the same pending echo/key and check
                    // whether it's actually finished now.
                    _state.value = _state.value.copy(
                        sending = false,
                        pendingEchoUncertain = true,
                        error = "That question was already sent and may still be processing. Checking for the answer…",
                    )
                    loadMessages()
                }
                else -> {
                    _state.value = _state.value.copy(
                        sending = false,
                        pendingEcho = null,
                        pendingEchoUncertain = false,
                        error = "Couldn't send that question (code ${resp.code()}). Your draft wasn't lost -- retype it.",
                        composerText = echo.content,
                    )
                }
            }
        } catch (_: Exception) {
            // We don't know whether the server actually received this --
            // retryOnConnectionFailure is disabled specifically so we never
            // silently resend it. Surface the honest ambiguity rather than
            // guessing. pendingEcho is deliberately left set (not cleared) so
            // the question doesn't just vanish, and its key is reused if the
            // technician taps Retry -- so even a blind retry here can't
            // create a duplicate turn.
            _state.value = _state.value.copy(
                sending = false,
                pendingEchoUncertain = true,
                error = "Lost connection while sending. Tap Retry to check whether it went through.",
            )
        }
    }

    fun selectClarifyingMachine(machineId: Int) {
        viewModelScope.launch {
            _state.value = _state.value.copy(sending = true, error = null)
            try {
                val resp = ApiClient.service.setConversationMachine(conversationId, SetMachineRequest(machineId))
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(conversation = resp.body())
                    // Awaited directly (not refresh(), which launches a
                    // detached child coroutine and returns immediately) so
                    // `finally` below can't clear `sending` -- re-enabling the
                    // composer -- before the reload it's supposed to be
                    // gating has actually finished.
                    loadMessages()
                } else {
                    _state.value = _state.value.copy(error = "Couldn't set the machine (code ${resp.code()}).")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(error = "Can't reach the server. Check your connection.")
            } finally {
                _state.value = _state.value.copy(sending = false)
            }
        }
    }

    fun retry(messageId: Int) {
        viewModelScope.launch {
            _state.value = _state.value.copy(sending = true, error = null)
            try {
                val resp = ApiClient.service.retryAnswer(conversationId, messageId)
                if (resp.isSuccessful) {
                    val updated = resp.body()!!
                    _state.value = _state.value.copy(
                        sending = false,
                        messages = _state.value.messages.map { if (it.id == updated.id) updated else it },
                    )
                } else {
                    _state.value = _state.value.copy(sending = false, error = "Retry failed (code ${resp.code()}).")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(sending = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }

    fun submitFeedback(messageId: Int, rating: String) {
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.submitFeedback(messageId, com.hmwagner.techmanual.network.FeedbackRequest(rating))
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(feedbackGiven = _state.value.feedbackGiven + (messageId to rating), error = null)
                } else {
                    // Non-blocking (plan 10.4: feedback must never interrupt
                    // the repair task) -- same bottom banner as every other
                    // error here, not a dialog. The buttons stay tappable
                    // (feedbackGiven is untouched on failure) so a retry is
                    // just tapping again, not navigating anywhere. Found live
                    // (2026-08-25): this used to fail silently, so a flaky
                    // connection meant a technician's feedback just vanished
                    // with no indication anything went wrong.
                    _state.value = _state.value.copy(error = "Couldn't record that feedback (code ${resp.code()}). Try again.")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(error = "Couldn't record that feedback -- check your connection and try again.")
            }
        }
    }

    fun saveAnswer(messageId: Int) {
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.saveAnswer(messageId)
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(savedMessageIds = _state.value.savedMessageIds + messageId, error = null)
                } else {
                    _state.value = _state.value.copy(error = "Couldn't save that answer (code ${resp.code()}). Try again.")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(error = "Couldn't save that answer -- check your connection and try again.")
            }
        }
    }

    fun openCitation(citation: CitationOut) {
        viewModelScope.launch {
            _state.value = _state.value.copy(evidenceLoading = true, evidence = null, evidenceDocumentId = citation.document_id)
            try {
                val resp = ApiClient.service.getEvidence(citation.document_id, citation.chunk_id)
                _state.value = _state.value.copy(evidenceLoading = false, evidence = resp.body())
            } catch (_: Exception) {
                _state.value = _state.value.copy(evidenceLoading = false)
            }
        }
    }

    fun dismissEvidence() {
        _state.value = _state.value.copy(evidence = null, evidenceDocumentId = null)
    }

    class Factory(private val conversationId: Int) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : androidx.lifecycle.ViewModel> create(modelClass: Class<T>): T {
            return ChatViewModel(conversationId) as T
        }
    }
}
