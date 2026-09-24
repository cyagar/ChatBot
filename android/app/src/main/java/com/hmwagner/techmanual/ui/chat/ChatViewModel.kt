package com.hmwagner.techmanual.ui.chat

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.CitationOut
import com.hmwagner.techmanual.network.ConversationOut
import com.hmwagner.techmanual.network.EvidenceOut
import com.hmwagner.techmanual.network.FeedbackRequest
import com.hmwagner.techmanual.network.MessageIn
import com.hmwagner.techmanual.network.MessageOut
import com.hmwagner.techmanual.network.SetMachineRequest
import com.hmwagner.techmanual.network.describeError
import com.hmwagner.techmanual.network.describeErrorWithCode
import com.hmwagner.techmanual.util.PendingSendStore
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/**
 * The technician's own question while it has no answer yet. `id` is the
 * Idempotency-Key the question is sent under and stays fixed across retries;
 * the server stores it on the user message, which is how a reload matches the
 * question to its reply.
 */
data class LocalEcho(val id: String, val content: String)

data class ChatUiState(
    val conversation: ConversationOut? = null,
    val messages: List<MessageOut> = emptyList(),
    val pendingEcho: LocalEcho? = null,
    // The send ended without a response: whether the server received the
    // question is unknown. Retry resends it under the same key.
    val pendingEchoUncertain: Boolean = false,
    // The server has stored this question and has not finished answering it.
    val pendingEchoStillProcessing: Boolean = false,
    val composerText: String = "",
    val sending: Boolean = false,
    val loadingHistory: Boolean = true,
    val error: String? = null,
    val evidence: EvidenceOut? = null,
    val evidenceDocumentId: Int? = null,
    val evidenceLoading: Boolean = false,
    // A non-2xx or exception must not leave both evidence and this null,
    // or the sheet (gated on evidenceLoading || evidence != null) would
    // never even open -- the request would silently fail with no visible
    // error and no way to retry. evidenceCitation is retained so Retry can
    // redrive the exact same request without the caller re-supplying it.
    val evidenceError: String? = null,
    val evidenceCitation: CitationOut? = null,
    val savedMessageIds: Set<Int> = emptySet(),
    // Each rated message's own rating, keyed by message id -- mirrors
    // savedMessageIds so a reload shows "already rated" instead of
    // resetting the buttons to blank.
    val feedbackByMessageId: Map<Int, String> = emptyMap(),
    // Cursor for the page of messages older than the oldest one loaded.
    val earlierCursor: String? = null,
    val loadingEarlier: Boolean = false,
)

class ChatViewModel(
    private val conversationId: Int,
    private val pendingStore: PendingSendStore = PendingSendStore.current,
) : ViewModel() {
    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state

    // Negative, decrementing, and never reused for the lifetime of this
    // ViewModel -- guaranteed not to collide with a real server-assigned
    // message id (always a positive DB primary key), which matters because
    // LazyColumn keys list items on MessageOut.id (see ChatScreen).
    private var nextLocalMessageId = -1

    // True only while performSend()'s own POST is awaiting a response. A
    // concurrent reload must not replace `messages` in that window, or the
    // POST's answer would be appended on top of a list that already has it.
    private var askQuestionInFlight = false

    // Bumped by every reload; a response that arrives after a newer reload
    // started is discarded so an older page can never overwrite a newer one.
    private var loadGeneration = 0

    init {
        pendingStore.load(conversationId)?.let {
            _state.value = _state.value.copy(pendingEcho = it, pendingEchoUncertain = true)
        }
        refresh()
    }

    fun refresh() {
        viewModelScope.launch { loadMessages() }
    }

    // Split out from refresh() so selectClarifyingMachine() can await a reload
    // in the same coroutine; a detached reload would let its `finally` clear
    // `sending` before the reload finished.
    private suspend fun loadMessages() {
        val generation = ++loadGeneration
        _state.value = _state.value.copy(loadingHistory = true, error = null)
        try {
            // Best-effort: a failure here must never block the messages
            // themselves from loading.
            val convResp = try {
                ApiClient.service.getConversation(conversationId)
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                null
            }
            if (generation != loadGeneration) return
            if (convResp?.isSuccessful == true) {
                _state.value = _state.value.copy(conversation = convResp.body())
            }

            val msgs = ApiClient.service.getMessages(conversationId)
            if (generation != loadGeneration) return
            if (msgs.isSuccessful) {
                val loaded = msgs.body().orEmpty()
                val current = _state.value
                val pending = current.pendingEcho
                // A send whose POST has not returned owns pendingEcho and its
                // status exclusively.
                val sendInFlight = current.sending
                // The pending question is matched by its idempotency key, and
                // its answer by the reply link -- never by "the last row is an
                // assistant message".
                val questionRow = pending?.let { p ->
                    loaded.lastOrNull { it.role == "user" && it.idempotency_key == p.id }
                }
                val answered = questionRow != null &&
                    loaded.any { it.role == "assistant" && it.reply_to_message_id == questionRow.id }
                val stillUnanswered = pending != null && !sendInFlight && !answered
                // The server stored the question and has no reply yet. The
                // stored row is hidden so it is not drawn twice next to the
                // pending bubble.
                val acceptedAndProcessing = stillUnanswered && questionRow != null
                val displayMessages = if (acceptedAndProcessing) loaded.filter { it.id != questionRow!!.id } else loaded
                if (pending != null && !sendInFlight && answered) pendingStore.clear(conversationId)
                _state.value = current.copy(
                    messages = if (askQuestionInFlight) current.messages else displayMessages,
                    earlierCursor = if (askQuestionInFlight) current.earlierCursor else msgs.headers()["X-Next-Cursor"],
                    loadingHistory = false,
                    pendingEcho = if (sendInFlight) current.pendingEcho else if (stillUnanswered) pending else null,
                    pendingEchoUncertain = if (sendInFlight) current.pendingEchoUncertain else stillUnanswered && !acceptedAndProcessing,
                    pendingEchoStillProcessing = if (sendInFlight) current.pendingEchoStillProcessing else acceptedAndProcessing,
                    error = if (sendInFlight) {
                        current.error
                    } else if (acceptedAndProcessing) {
                        "That question was already sent and is still being answered. Tap Retry to check again."
                    } else if (stillUnanswered) {
                        "Still waiting to hear back on that question. Pull to refresh or tap Retry to check again."
                    } else null,
                    // Rehydrated from the server so a re-created ViewModel does
                    // not reset saved/rated marks and invite duplicate actions.
                    savedMessageIds = loaded.filter { it.is_saved }.map { it.id }.toSet(),
                    feedbackByMessageId = loaded.mapNotNull { m -> m.feedback_rating?.let { m.id to it } }.toMap(),
                )
            } else {
                _state.value = _state.value.copy(loadingHistory = false, error = msgs.describeError("Couldn't load this conversation"))
            }
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            if (generation == loadGeneration) {
                _state.value = _state.value.copy(loadingHistory = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }

    fun loadEarlier() {
        val cursor = _state.value.earlierCursor ?: return
        if (_state.value.loadingEarlier) return
        _state.value = _state.value.copy(loadingEarlier = true)
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.getMessages(conversationId, before = cursor)
                if (resp.isSuccessful) {
                    val loadedIds = _state.value.messages.map { it.id }.toSet()
                    val older = resp.body().orEmpty().filter { it.id !in loadedIds }
                    _state.value = _state.value.copy(
                        messages = older + _state.value.messages,
                        earlierCursor = resp.headers()["X-Next-Cursor"],
                        loadingEarlier = false,
                    )
                } else {
                    _state.value = _state.value.copy(
                        loadingEarlier = false,
                        error = resp.describeError("Couldn't load earlier messages"),
                    )
                }
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                _state.value = _state.value.copy(
                    loadingEarlier = false,
                    error = "Can't reach the server. Check your connection.",
                )
            }
        }
    }

    fun onComposerChange(v: String) {
        _state.value = _state.value.copy(composerText = v)
    }

    // One question at a time: while a question has no answer yet, a second
    // one is not sent.
    fun send() {
        val text = _state.value.composerText.trim()
        if (text.isEmpty() || _state.value.sending || _state.value.pendingEcho != null) return

        val echo = LocalEcho(UUID.randomUUID().toString(), text)
        pendingStore.save(conversationId, echo)
        _state.value = _state.value.copy(
            sending = true,
            error = null,
            composerText = "",
            pendingEcho = echo,
            pendingEchoUncertain = false,
            pendingEchoStillProcessing = false,
        )
        viewModelScope.launch { performSend(echo) }
    }

    /**
     * Resends the pending question under the SAME idempotency key and text --
     * never a fresh key, never the composer's current content. If the original
     * attempt landed, the server replays its answer (or resumes it, if the
     * attempt died) instead of creating a second turn.
     */
    fun retryPendingSend() {
        val echo = _state.value.pendingEcho ?: return
        if (_state.value.sending) return
        _state.value = _state.value.copy(
            sending = true,
            error = null,
            pendingEchoUncertain = false,
            pendingEchoStillProcessing = false,
        )
        viewModelScope.launch { performSend(echo) }
    }

    private fun rejectPendingQuestion(echo: LocalEcho, message: String) {
        pendingStore.clear(conversationId)
        // Restore the question to the composer only if nothing newer has been
        // typed since.
        val newerDraftTyped = _state.value.composerText.isNotEmpty()
        _state.value = _state.value.copy(
            sending = false,
            pendingEcho = null,
            pendingEchoUncertain = false,
            pendingEchoStillProcessing = false,
            error = if (newerDraftTyped) message else "$message Your draft wasn't lost -- retype it.",
            composerText = if (newerDraftTyped) _state.value.composerText else echo.content,
        )
    }

    private suspend fun performSend(echo: LocalEcho) {
        try {
            val resp = try {
                askQuestionInFlight = true
                ApiClient.service.askQuestion(conversationId, MessageIn(echo.content), idempotencyKey = echo.id)
            } finally {
                askQuestionInFlight = false
            }
            if (resp.isSuccessful) {
                val answer = resp.body()!!
                pendingStore.clear(conversationId)
                // The server never returns the user's own message from this
                // endpoint, so it is folded into `messages` as a local turn
                // ahead of the answer.
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
                    pendingEchoStillProcessing = false,
                )
                return
            }
            val failure = resp.describeErrorWithCode("Couldn't send that question")
            if (failure.code == "IDEMPOTENCY_IN_PROGRESS") {
                // This exact question is stored and being answered; keep it
                // pending under the same key and check on it.
                _state.value = _state.value.copy(sending = false)
                loadMessages()
            } else {
                // Any other rejection -- a busy conversation, a superseded or
                // mismatched key, a validation or server error -- means this
                // question was not accepted for answering.
                rejectPendingQuestion(echo, failure.message)
            }
        } catch (e: CancellationException) {
            throw e
        } catch (_: Exception) {
            // Whether the server received the question is unknown; it stays
            // pending under its key so Retry can resolve it safely.
            _state.value = _state.value.copy(
                sending = false,
                pendingEchoUncertain = true,
                pendingEchoStillProcessing = false,
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
                    _state.value = _state.value.copy(error = resp.describeError("Couldn't set the machine"))
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
                    _state.value = _state.value.copy(sending = false, error = resp.describeError("Retry failed"))
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(sending = false, error = "Can't reach the server. Check your connection.")
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
                    _state.value = _state.value.copy(error = "${resp.describeError("Couldn't save that answer")} Try again.")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(error = "Couldn't save that answer -- check your connection and try again.")
            }
        }
    }

    // Lets a technician undo a save from the chat screen.
    fun unsaveAnswer(messageId: Int) {
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.unsaveAnswer(messageId)
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(savedMessageIds = _state.value.savedMessageIds - messageId, error = null)
                } else {
                    _state.value = _state.value.copy(error = "${resp.describeError("Couldn't remove that saved answer")} Try again.")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(error = "Couldn't remove that saved answer -- check your connection and try again.")
            }
        }
    }

    fun submitFeedback(messageId: Int, rating: String, comment: String? = null) {
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.submitFeedback(messageId, FeedbackRequest(rating, comment))
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(
                        feedbackByMessageId = _state.value.feedbackByMessageId + (messageId to rating),
                        error = null,
                    )
                } else {
                    _state.value = _state.value.copy(error = "${resp.describeError("Couldn't record that feedback")} Try again.")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(error = "Couldn't record that feedback -- check your connection and try again.")
            }
        }
    }

    // Each citation tap must cancel any previous load and never let a stale
    // response apply -- without that, tapping citation B while A was still
    // loading could let A's response land last and overwrite B's
    // evidence/page under B's still-displayed citation header, a direct
    // safety risk since technicians use citations to verify manual
    // instructions. Guarded two ways: cancelling the previous load's Job
    // before starting a new one (covers both a new tap and dismissal), and
    // comparing a monotonically increasing request token before applying
    // any success/error, so even a response that slips past cancellation
    // (already in flight when cancel() was called) is ignored if it's not
    // for the request that's still current.
    private var evidenceJob: kotlinx.coroutines.Job? = null
    private var evidenceRequestToken = 0

    fun openCitation(citation: CitationOut) {
        _state.value = _state.value.copy(
            evidenceCitation = citation,
            evidenceLoading = true,
            evidence = null,
            evidenceError = null,
            evidenceDocumentId = citation.document_id,
        )
        loadEvidence(citation)
    }

    /** Redrives the same request as the last [openCitation] call, without losing the citation context. */
    fun retryEvidence() {
        val citation = _state.value.evidenceCitation ?: return
        _state.value = _state.value.copy(evidenceLoading = true, evidence = null, evidenceError = null)
        loadEvidence(citation)
    }

    private fun loadEvidence(citation: CitationOut) {
        evidenceJob?.cancel()
        val token = ++evidenceRequestToken
        evidenceJob = viewModelScope.launch {
            try {
                val resp = ApiClient.service.getEvidence(citation.document_id, citation.chunk_id)
                if (token != evidenceRequestToken) return@launch
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(evidenceLoading = false, evidence = resp.body())
                } else {
                    _state.value = _state.value.copy(
                        evidenceLoading = false,
                        evidenceError = resp.describeError("Couldn't load this evidence"),
                    )
                }
            } catch (e: kotlinx.coroutines.CancellationException) {
                // A newer citation tap (or dismiss) cancelled THIS job via
                // evidenceJob?.cancel() above -- that's expected, cooperative
                // cancellation, not a failure. Rethrowing (rather than
                // falling into the generic catch below, which used to treat
                // this identically to a real network error and apply a
                // stale error state on top of whatever the newer
                // request/dismiss had already set) is required for
                // structured concurrency regardless.
                throw e
            } catch (_: Exception) {
                if (token != evidenceRequestToken) return@launch
                _state.value = _state.value.copy(
                    evidenceLoading = false,
                    evidenceError = "Can't reach the server. Check your connection.",
                )
            }
        }
    }

    fun dismissEvidence() {
        evidenceJob?.cancel()
        evidenceRequestToken++
        _state.value = _state.value.copy(
            evidence = null,
            evidenceDocumentId = null,
            evidenceError = null,
            evidenceCitation = null,
            // Also reset here (pre-existing gap, not previously reachable
            // without the token/cancellation fix above): dismissing while a
            // load was still in flight used to leave evidenceLoading=true
            // forever, since only openCitation/loadEvidence ever set it.
            evidenceLoading = false,
        )
    }

    class Factory(private val conversationId: Int) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : androidx.lifecycle.ViewModel> create(modelClass: Class<T>): T {
            return ChatViewModel(conversationId) as T
        }
    }
}
